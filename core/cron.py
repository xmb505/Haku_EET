"""
cron.py —— 事件驱动延时定时器

支持两种事件规则：
  - 'reschedule': IO 信号触发时重新推迟触发时间
  - 'cancel':     IO 信号触发时自毁销毁 job

设计原则：
  - 纯事件驱动（asyncio.Event wakeup，零轮询）
  - Job 按触发时间小顶堆排序
  - 懒删除：cancel/auto_remove 只删 _jobs 和事件索引，heap 老 entry 在 fire 时跳过
  - 重调度记录最新 trigger_time，旧 heap entry 自动跳过
  - IO listener 在首次 schedule 含 event_rules 的 job 时注册
"""

import asyncio
import heapq
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class EventRule:
    """IO 事件触发时的处理规则

    Attributes:
        signal_name: IO 信号名（如 'light_curtain', 'cabin_button_5'）
        car_id: 轿厢 ID
        action: 'reschedule' | 'cancel'
        delay: 仅 'reschedule' 有效：重设为 now + delay
    """
    signal_name: str
    car_id: int
    action: str  # 'reschedule' | 'cancel'
    delay: float = 0.0


@dataclass
class CronJob:
    """计划任务

    Attributes:
        name: 唯一标识（同名 job 会覆盖旧 job）
        trigger_time: 触发绝对时间（time.monotonic）
        action: 触发时执行的异步回调
        delay: 基础延时（供 reschedule 从 now 重算）
        event_rules: 监听哪些 IO 信号，触发时如何反应
    """
    name: str
    trigger_time: float
    action: Callable[[], Awaitable[None]]
    delay: float = 0.0
    event_rules: list[EventRule] | None = None


class Cron:
    """事件驱动延时定时器

    用法:
        cron = Cron()
        await cron.schedule(CronJob(name='x', trigger_time=..., action=...))
        await cron.start()  # 后台运行
        ...
        await cron.stop()
    """

    def __init__(self) -> None:
        # 活跃 job
        self._jobs: dict[str, CronJob] = {}
        # 小顶堆 [(trigger_time, name)]
        self._heap: list[tuple[float, str]] = []
        # 事件索引 {(car_id, signal): [(job_name, action, delay)]}
        self._event_idx: dict[tuple[int, str], list[tuple[str, str, float]]] = {}
        # 最新 trigger_time（用于检测 stale heap entry）
        self._latest_trigger: dict[str, float] = {}

        self._wakeup_event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None
        self._listener_ref: Callable | None = None
        self._io = None
        self._mapper = None

    # ===== 公共 API =====

    async def schedule(self, job: CronJob) -> None:
        """调度一个 job。同名 job 先取消旧的再建新的。"""
        # 取消同名旧 job
        old = self._jobs.pop(job.name, None)
        if old is not None:
            self._clean_event_index(job.name, old)

        self._jobs[job.name] = job
        self._latest_trigger[job.name] = job.trigger_time
        heapq.heappush(self._heap, (job.trigger_time, job.name))

        # 注册事件规则
        if job.event_rules:
            for rule in job.event_rules:
                key = (rule.car_id, rule.signal_name)
                if key not in self._event_idx:
                    self._event_idx[key] = []
                self._event_idx[key].append(
                    (job.name, rule.action, rule.delay)
                )
            self._ensure_listener()

        # 唤醒主循环（新 job 可能早于当前等待）
        self._wakeup_event.set()

    async def cancel(self, name: str) -> None:
        """取消一个 job"""
        job = self._jobs.pop(name, None)
        if job is not None:
            self._clean_event_index(name, job)
        self._latest_trigger.pop(name, None)

    def register(self, io, mapper) -> None:
        """注册 IO listener（由 app.py 在 start() 时调用）"""
        self._io = io
        self._mapper = mapper

    # ===== 生命周期 =====

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        self._wakeup_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._jobs.clear()
        self._heap.clear()
        self._event_idx.clear()
        self._latest_trigger.clear()

    # ===== 主循环 =====

    async def _run(self) -> None:
        """主循环：fire 过期 job → 等待下一 deadline（事件驱动，零轮询）"""
        while self._running:
            now = time.monotonic()

            # Fire all expired jobs
            while self._heap and self._heap[0][0] <= now:
                _, name = heapq.heappop(self._heap)

                # 跳过 stale entry（job 被 reschedule 过，有新 trigger_time）
                expected = self._latest_trigger.get(name)
                if expected is not None and time.monotonic() < expected:
                    continue

                job = self._jobs.pop(name, None)
                if job is None:
                    continue
                self._latest_trigger.pop(name, None)
                self._clean_event_index(name, job)
                try:
                    await job.action()
                except Exception as e:
                    print(f'[cron] job {name!r} error: {e!r}')

            # Wait until next deadline or wakeup
            self._wakeup_event.clear()
            if self._heap:
                wait = max(0.001, self._heap[0][0] - time.monotonic())
                try:
                    await asyncio.wait_for(
                        self._wakeup_event.wait(), timeout=wait
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await self._wakeup_event.wait()

    # ===== IO 事件处理 =====

    async def _on_io_event(self, event) -> None:
        """IO listener：匹配 event index → reschedule / cancel"""
        if event.bit != 1:
            return
        if self._mapper is None:
            return

        sig = self._mapper.lookup_signal_by_i(event.i_addr)
        if sig is None:
            return
        cid, signal_name = sig

        key = (cid, signal_name)
        actions = self._event_idx.get(key)
        if actions is None:
            return

        now = time.monotonic()
        for job_name, action_type, delay in actions:
            if action_type == 'reschedule':
                job = self._jobs.get(job_name)
                if job is None:
                    continue
                new_trigger = now + delay
                job.trigger_time = new_trigger
                self._latest_trigger[job_name] = new_trigger
                heapq.heappush(self._heap, (new_trigger, job_name))
                self._wakeup_event.set()
            elif action_type == 'cancel':
                job = self._jobs.pop(job_name, None)
                if job is not None:
                    self._clean_event_index(job_name, job)
                    self._latest_trigger.pop(job_name, None)

    def _ensure_listener(self) -> None:
        """注册 IO listener（首次有 event_rules 的 job 时调用）"""
        if self._listener_ref is not None or self._io is None:
            return
        self._listener_ref = self._on_io_event
        self._io.add_listener(self._listener_ref)

    def _clean_event_index(self, name: str, job: CronJob) -> None:
        """从事件索引移除 job 的所有条目"""
        if not job.event_rules:
            return
        for rule in job.event_rules:
            key = (rule.car_id, rule.signal_name)
            entries = self._event_idx.get(key)
            if entries is not None:
                self._event_idx[key] = [
                    e for e in entries if e[0] != name
                ]
                if not self._event_idx[key]:
                    del self._event_idx[key]
