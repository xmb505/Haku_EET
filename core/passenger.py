"""
passenger.py —— 乘客交互管理器（大脑）

分层定位：
  ┌─────────────────────────────────────────┐
  │  大脑                            本文件  │
  │  乘客流程管理：关门cron/队列/指示灯       │
  ├─────────────────────────────────────────┤
  │  小脑 app.py                              │
  │  IO事件路由 / 高层API / Action编排        │
  ├─────────────────────────────────────────┤
  │  脑干 executor/algorithm/controllers     │
  │  Action→IO序列 / 硬件展开                │
  └─────────────────────────────────────────┘

设计原则:
  - 大脑不注册任何 IO 监听器，不接触任何 IO 事件
  - 大脑只通过小脑 app.py 的 API 交互（call_internal, action_queues.put, ui.set_xxx）
  - 外召/内召/门按钮/光幕等原始 IO 事件由小脑 app.py 处理，
    处理完后再调用大脑的流程管理方法
  - 拥有独立的乘客请求队列（PassengerQueue），不污染 pending_calls
"""

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .actions import Action, ActionKind
from .cron import CronJob, EventRule
from .player import CarState, Direction, DoorState

if TYPE_CHECKING:
    from .app import App


class PassengerQueue:
    """乘客请求队列（独立于小脑 pending_calls）

    两种模式:
      'discard': 顺向未过站接受，已过站丢弃
      'keep':    全部保留，到达当前目标后继续处理

    使用方法:
      1. 开门期间收集内召到 cache
      2. 关门后 compile(cache) → 生成 _items 路线
      3. 逐个 next() 消费，mark_served() 标记完成
    """

    def __init__(self, mode: str = 'discard') -> None:
        if mode not in ('discard', 'keep'):
            raise ValueError(f"乘客队列模式必须为 'discard' 或 'keep'，收到 {mode!r}")
        self.mode = mode
        self._items: list[int] = []

    def compile(self, cache: set[int], car_position: int,
                car_direction: Direction, current_target: int | None) -> None:
        self._items.clear()
        if not cache:
            return

        pos = car_position
        target = current_target

        if self.mode == 'discard':
            if car_direction == Direction.UP and target is not None:
                valid = [f for f in cache if pos < f <= target]
                valid.sort()
                self._items = valid
            elif car_direction == Direction.DOWN and target is not None:
                valid = [f for f in cache if pos > f >= target]
                valid.sort(reverse=True)
                self._items = valid
            else:
                self._items = sorted(cache)
        else:
            floors = sorted(cache)
            if car_direction == Direction.UP and target is not None:
                forward = [f for f in floors if pos < f <= target]
                backward = [f for f in floors if f <= pos or f > target]
                self._items = forward + backward
            elif car_direction == Direction.DOWN and target is not None:
                forward = [f for f in floors if pos > f >= target]
                backward = [f for f in floors if f >= pos or f < target]
                self._items = list(reversed(forward)) + list(reversed(backward))
            else:
                self._items = floors

    @property
    def items(self) -> list[int]:
        return list(self._items)

    def next_target(self) -> int | None:
        return self._items[0] if self._items else None

    def mark_served(self, floor: int) -> None:
        if floor in self._items:
            self._items.remove(floor)

    def clear(self) -> None:
        self._items.clear()

    def __bool__(self) -> bool:
        return bool(self._items)

    def __len__(self) -> int:
        return len(self._items)


class PassengerManager:
    """乘客流程管理器（大脑）

    只提供纯流程管理方法，由小脑 app.py 在事件处理完成后调用。
    不注册任何 IO 监听器。

    对外接口（由 app.py 调用）:
      on_hall_call_serving(cid, floor, dir)   派车后——标记接客、亮外召灯
      on_hall_call_release(cid, floor, dir)   外召松开——启动关门 cron
      on_cabin_button_door_open(cid, floor)   门开着时内召——缓存
      on_light_curtain(cid)                   光幕——重调度关门 cron
      on_action_done(cid, action)             小脑动作完成通知
      on_door_button_change(cid, sig, bit)    门按钮——开门/关门
      reset(cid)                              全状态重置
      on_emergency(cid)                       紧急停止清理
      status_snapshot(cid)                    状态快照
    """

    def __init__(self, app: 'App', ui_config_path: str | Path) -> None:
        self._app = app
        self._ui_config_path = Path(ui_config_path)
        self._ui_config: dict = {}
        self._reload_ui_config()

        # ==== per-car 状态 ====
        self._passenger_queue: dict[int, PassengerQueue] = {}
        self._button_cache: dict[int, set[int]] = {}
        self._pickup_active: dict[int, dict[tuple[int, str], bool]] = {}
        self._flash_tasks: dict[int, dict[str, asyncio.Task]] = {}

        for cid in app.car_ids:
            self._passenger_queue[cid] = PassengerQueue(
                mode=self._ui_config.get('passenger', {}).get('queue_mode', 'discard')
            )
            self._button_cache[cid] = set()
            self._pickup_active[cid] = {}
            self._flash_tasks[cid] = {}

    def _reload_ui_config(self) -> None:
        try:
            with self._ui_config_path.open('r', encoding='utf-8') as f:
                self._ui_config = yaml.safe_load(f) or {}
        except (FileNotFoundError, yaml.YAMLError) as e:
            print(f'[pm] ui_config 加载失败: {e}，使用默认值')
            self._ui_config = {}

    # ===== config helpers =====

    def _door_close_delay(self) -> float:
        return self._ui_config.get('passenger', {}).get('door_close_delay', 10.0)

    def _light_off_delay(self) -> float:
        return self._ui_config.get('passenger', {}).get('light_off_delay', 600.0)

    def _flash_interval(self) -> float:
        ms = self._ui_config.get('passenger', {}).get('flash_interval_ms', 500)
        return max(0.05, ms / 1000.0)

    def _queue_mode(self) -> str:
        return self._ui_config.get('passenger', {}).get('queue_mode', 'discard')

    @property
    def queue_mode(self) -> str:
        return self._queue_mode()

    def set_queue_mode(self, mode: str) -> None:
        """切换队列模式并写回 ui_config.yaml"""
        if mode not in ('discard', 'keep'):
            raise ValueError(f"队列模式必须为 'discard' 或 'keep'，收到 {mode!r}")
        for cid in self._app.car_ids:
            self._passenger_queue[cid].mode = mode
        # 写回文件
        self._ui_config.setdefault('passenger', {})['queue_mode'] = mode
        with self._ui_config_path.open('w', encoding='utf-8') as f:
            yaml.safe_dump(self._ui_config, f, allow_unicode=True)

    # ===== 大脑决策方法（由小脑 app.py 转发 IO 事件） =====

    async def on_hall_call(self, floor: int, direction: str, bit: int) -> None:
        """hall call button event (forwarded from app.py IO parser)

        bit=1: dispatch car → open door if at floor, else call_internal
        bit=0: start close-door cron if door is open
        """
        if bit == 1:
            target_cid = self._app._dispatch_hall_call(floor, direction)
            if target_cid is None:
                print(f'[hall_call] {direction}@L{floor} no available car')
                return
            # mark pickup, light indicator
            self._pickup_active[target_cid][(floor, direction)] = True
            await self._app.set_hall_indicator(floor, direction, True)
            car = self._app.cars[target_cid]
            if car.position == floor:
                await self._app.action_queues[target_cid].put(
                    Action(ActionKind.OPEN_DOOR))
                print(f'[hall_call] {direction}@L{floor} → car{target_cid} at floor, opening')
            else:
                await self._app.call_internal(floor, car_id=target_cid, origin='hall')
                print(f'[hall_call] {direction}@L{floor} → car{target_cid}')
        else:
            # bit=0: find car serving this pickup → start close cron
            for cid in self._app.car_ids:
                if self._pickup_active.get(cid, {}).get((floor, direction), False):
                    car = self._app.cars[cid]
                    if car.door_state == DoorState.OPEN:
                        await self._start_close_door_cron(cid, floor, direction)
                    return

    async def on_cabin_button(self, cid: int, floor: int) -> None:
        """cabin button: door open → cache; door closed → call_internal"""
        car = self._app.cars[cid]
        if car.door_state in (DoorState.OPEN, DoorState.OPENING):
            self._button_cache[cid].add(floor)
            await self._app.cron.cancel(self._close_door_job_name(cid))
        else:
            await self._app.call_internal(floor, car_id=cid)

    async def on_door_button(self, cid: int, signal: str) -> None:
        """door button: open/close relay directly"""
        car = self._app.cars[cid]
        if signal == 'door_open_button':
            await self._app.cron.cancel(self._close_door_job_name(cid))
            if car.door_state in (DoorState.CLOSED, DoorState.CLOSING):
                await self._app.action_queues[cid].put(
                    Action(ActionKind.OPEN_DOOR))
        elif signal == 'door_close_button':
            await self._app.cron.cancel(self._close_door_job_name(cid))
            if car.door_state in (DoorState.OPEN, DoorState.OPENING):
                await self._app.action_queues[cid].put(
                    Action(ActionKind.CLOSE_DOOR))

    # ===== 小脑回调方法 =====

    async def on_light_curtain(self, car_id: int) -> None:
        """light curtain triggered → reschedule close cron (preserving hall signals)"""
        await self._app.cron.cancel(self._close_door_job_name(car_id))
        hall = [(f, d) for (f, d), a in self._pickup_active[car_id].items() if a]
        await self._schedule_close_door_cron_job(
            car_id, self._close_door_job_name(car_id),
            hall_signals=hall if hall else None)

    async def on_light_curtain_release(self, car_id: int) -> None:
        """light curtain released → re-arm close cron if door is open"""
        car = self._app.cars[car_id]
        if car.door_state != DoorState.OPEN:
            return
        hall = [(f, d) for (f, d), a in self._pickup_active[car_id].items() if a]
        await self._schedule_close_door_cron_job(
            car_id, self._close_door_job_name(car_id),
            hall_signals=hall if hall else None)

    async def on_breach(self, car_id: int) -> None:
        """breach event: light curtain triggered during close → door reversed to open
        
        Register close cron with light-curtain-based self-destruct.
        The cron will only fire when the light curtain is clear at dispatch time.
        """
        hall = [(f, d) for (f, d), a in self._pickup_active[car_id].items() if a]
        await self._schedule_close_door_cron_job(
            car_id, self._close_door_job_name(car_id),
            hall_signals=hall if hall else None)

    # ===== 门流程（由 on_action_done 驱动） =====

    async def on_action_done(self, car_id: int, action: Action) -> None:
        """小脑动作完成通知（由 app._on_action_done 调用）"""
        if action.kind == ActionKind.OPEN_DOOR:
            await self._on_door_opened(car_id)
        elif action.kind == ActionKind.CLOSE_DOOR:
            await self._on_door_closed(car_id)
        elif action.kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            await self._on_move_done(car_id)

    async def _on_move_done(self, car_id: int) -> None:
        """MOVE 到站 → 标记服务完成 + 队列还有剩余则开门接客

        用 car.position 与乘客队列匹配，不依赖 car.target_floor
        （因为 _handle_algorithm_state_change 会先清 target_floor）。
        """
        car = self._app.cars[car_id]
        pq = self._passenger_queue[car_id]
        if not pq:
            return
        pos = car.position
        if pos is not None and pos in set(pq.items):
            pq.mark_served(pos)
            if pq:
                await self._app.action_queues[car_id].put(
                    Action(ActionKind.OPEN_DOOR))

    async def _on_door_opened(self, car_id: int) -> None:
        """door opened → clear indicators and button cache only"""
        app = self._app
        for (floor, direction), active in list(
                self._pickup_active[car_id].items()):
            if active:
                await app.set_hall_indicator(floor, direction, False)
                flash_key = f'{direction}_{floor}'
                task = self._flash_tasks[car_id].pop(flash_key, None)
                if task is not None and not task.done():
                    task.cancel()
        self._button_cache[car_id] = set()

    async def _on_door_closed(self, car_id: int) -> None:
        """门已关闭 → 清接客状态、合并队列 → 出发或熄灯"""
        self._pickup_active[car_id].clear()

        car = self._app.cars[car_id]
        pq = self._passenger_queue[car_id]

        # 合并已有队列余项 + 本次开门期间的新内召缓存
        all_requests = set(pq.items) | self._button_cache[car_id]
        pq.compile(
            cache=all_requests,
            car_position=car.position,
            car_direction=car.direction,
            current_target=car.target_floor,
        )
        self._button_cache[car_id].clear()

        if pq:
            first = pq.next_target()
            if first is not None:
                await self._app.call_internal(first, car_id=car_id)
        else:
            await self._start_lights_off_cron(car_id)

    # ===== 关门 cron =====

    def _close_door_job_name(self, car_id: int) -> str:
        return f'pm_car{car_id}_close_door'

    def _lights_off_job_name(self, car_id: int) -> str:
        return f'pm_car{car_id}_lights_off'

    async def _start_close_door_cron(self, car_id: int,
                                      floor: int, direction: str) -> None:
        jn = self._close_door_job_name(car_id)
        await self._app.cron.cancel(jn)
        await self._schedule_close_door_cron_job(car_id, jn, floor, direction)

    async def _schedule_close_door_cron_job(
        self, car_id: int, job_name: str,
        floor: int | None = None, direction: str | None = None,
        hall_signals: list[tuple[int, str]] | None = None
    ) -> None:
        delay = self._door_close_delay()
        event_rules = [
            EventRule('door_open_button', car_id, 'cancel', 0),
            EventRule('door_close_button', car_id, 'cancel', 0),
        ]
        # 外召按下 → 自毁关门 cron（长按保持开门）
        if hall_signals:
            for f, d in hall_signals:
                event_rules.append(
                    EventRule(f'hall_call_{d}_{f}', 0, 'cancel', 0))
        elif floor is not None and direction is not None:
            event_rules.append(
                EventRule(f'hall_call_{direction}_{floor}', 0, 'cancel', 0))

        async def _close_door_action():
            car = self._app.cars[car_id]
            if car.door_state not in (DoorState.OPEN, DoorState.OPENING):
                return
            # check light curtain before closing: if still blocked, reschedule
            try:
                lc_addr = self._app.mapper.db_to_i(
                    self._app.mapper.addr_input('light_curtain', car_id))
                if self._app.io.get_input(lc_addr) == 1:
                    await self._app.cron.cancel(job_name)
                    await self._schedule_close_door_cron_job(
                        car_id, job_name, floor, direction,
                        hall_signals=hall_signals)
                    return
            except KeyError:
                pass
            await self._app.action_queues[car_id].put(
                    Action(ActionKind.CLOSE_DOOR))

        await self._app.cron.schedule(CronJob(
            name=job_name,
            trigger_time=time.monotonic() + delay,
            delay=delay,
            action=_close_door_action,
            event_rules=event_rules,
        ))

    # ===== 熄灯节能 cron =====

    async def _start_lights_off_cron(self, car_id: int) -> None:
        jn = self._lights_off_job_name(car_id)
        await self._app.cron.cancel(jn)
        delay = self._light_off_delay()

        async def _lights_off_action():
            car = self._app.cars[car_id]
            car.human_presence = -1
            await self._app.ui[car_id].set_light(False)

        event_rules = [
            EventRule('door_open_button', car_id, 'cancel', 0),
        ]
        await self._app.cron.schedule(CronJob(
            name=jn,
            trigger_time=time.monotonic() + delay,
            delay=delay,
            action=_lights_off_action,
            event_rules=event_rules,
        ))

    # ===== 外召指示灯闪烁 =====

    async def _start_hall_indicator_flash(self, car_id: int,
                                           floor: int, direction: str) -> None:
        flash_key = f'{direction}_{floor}'
        old = self._flash_tasks[car_id].get(flash_key)
        if old is not None and not old.done():
            old.cancel()

        async def _flash_loop():
            app = self._app
            interval = self._flash_interval()
            try:
                while True:
                    await app.set_hall_indicator(floor, direction, True)
                    await asyncio.sleep(interval)
                    await app.set_hall_indicator(floor, direction, False)
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(_flash_loop())

        def _on_flash_done(t):
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                print(f'[passenger] flash_loop car{car_id} 异常: {exc!r}')

        task.add_done_callback(_on_flash_done)
        self._flash_tasks[car_id][flash_key] = task

    # ===== 状态查询 =====

    def status_snapshot(self, car_id: int) -> dict:
        car = self._app.cars[car_id]
        return {
            'enabled': True,  # 由 app.usermode_enabled 决定
            'queue_mode': self._queue_mode(),
            'button_cache': sorted(self._button_cache.get(car_id, set())),
            'passenger_queue': self._passenger_queue.get(car_id, PassengerQueue()).items,
            'pickup_active': [(f, d) for (f, d), v in
                              self._pickup_active.get(car_id, {}).items() if v],
            'human_presence': car.human_presence,
        }

    # ===== 重置 / 紧急停止 =====

    async def reset(self, car_id: int) -> None:
        await self._reset_state(car_id)
        pq = self._passenger_queue[car_id]
        pq.mode = self._queue_mode()

    async def on_emergency(self, car_id: int) -> None:
        await self._reset_state(car_id)

    async def _reset_state(self, car_id: int) -> None:
        self._button_cache[car_id] = set()
        self._passenger_queue[car_id].clear()
        # 先关灯（读 _pickup_active），再清字典
        await self._clear_floor_indicator(car_id)
        self._pickup_active[car_id].clear()
        await self._app.cron.cancel(self._close_door_job_name(car_id))
        await self._app.cron.cancel(self._lights_off_job_name(car_id))

    async def _clear_floor_indicator(self, car_id: int) -> None:
        app = self._app
        for (floor, direction), active in list(
                self._pickup_active[car_id].items()):
            if active:
                await app.set_hall_indicator(floor, direction, False)
                flash_key = f'{direction}_{floor}'
                task = self._flash_tasks[car_id].pop(flash_key, None)
                if task is not None and not task.done():
                    task.cancel()
            self._pickup_active[car_id][(floor, direction)] = False
