"""
watchdog.py —— 独立看门狗：监控 CLOSING 卡死

完全独立于 executor 的 action loop，以固定间隔轮询每部车的门状态。
唯一使命：如果 CLOSING 持续超过阈值，检查 door_close_done 信号，
若为 1 则强制完成关门（解除 wait_done 阻塞）。

设计哲学：
  - 不参与调度、不修改 Car 属性、不推 Action
  - 只做一件事：解除 executor 的 wait_done 死锁
  - 通过 door.cancel() 设置 _done event，让 executor 自然走完 CLOSE_DOOR 后续流程
"""

import asyncio
import time
from typing import TYPE_CHECKING

from .player import DoorState

if TYPE_CHECKING:
    from .app import App


class DoorWatchdog:
    """独立看门狗：CLOSING 超时 → 检查 door_close_done → 强制完成"""

    def __init__(self, app: 'App', timeout: float = 5.0, interval: float = 1.0):
        """
        Args:
            app: App 实例（用于访问 executors / io / mapper）
            timeout: CLOSING 超过此秒数视为卡死（默认 5s，比 executor 自身的 8s 更短）
            interval: 轮询间隔（默认 1s）
        """
        self._app = app
        self._timeout = timeout
        self._interval = interval
        self._closing_since: dict[int, float] = {}  # car_id → 进入 CLOSING 的时间
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name='door_watchdog')

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._check_all()
            except Exception as e:
                # 看门狗自身不能崩
                self._log(f'[watchdog] error: {e}')

    async def _check_all(self) -> None:
        now = time.monotonic()
        for cid in self._app.car_ids:
            car = self._app.cars[cid]
            exe = self._app.executors.get(cid)
            if exe is None:
                continue

            if car.door_state == DoorState.CLOSING:
                if cid not in self._closing_since:
                    self._closing_since[cid] = now
                elif now - self._closing_since[cid] > self._timeout:
                    await self._try_force_close(cid, exe)
            else:
                self._closing_since.pop(cid, None)

    async def _try_force_close(self, cid: int, exe) -> None:
        """检查 door_close_done，若为 1 则强制完成关门"""
        try:
            addr = self._app.mapper.addr_input('door_close_done', cid)
            close_done = self._app.io.get_input(addr)
        except KeyError:
            return

        confirmed = close_done == 1
        if not confirmed:
            try:
                lock_addr = self._app.mapper.addr_input('car_door_lock', cid)
                if self._app.io.get_input(lock_addr) == 1:
                    confirmed = True
            except KeyError:
                pass

        if not confirmed:
            return

        car = self._app.cars[cid]
        if exe.current_action is None:
            # 无活跃动作 = 残留状态（executor 已走完但 door_state 未更新）
            self._log(f'[watchdog] car{cid} CLOSING 超时 {self._timeout}s，'
                      f'IO 确认已关 + 无活跃动作 → 直接修正 door_state')
            car.door_state = DoorState.CLOSED
        else:
            self._log(f'[watchdog] car{cid} CLOSING 超时 {self._timeout}s，'
                      f'IO 确认已关 → 强制完成关门')
            exe.door.cancel()
        self._closing_since.pop(cid, None)

    def _log(self, msg: str) -> None:
        if hasattr(self._app, '_log_file') and hasattr(self._app._log_file, 'write'):
            self._app._log_file.write(msg + '\n')
            self._app._log_file.flush()
