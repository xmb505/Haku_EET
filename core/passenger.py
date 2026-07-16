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
import sys
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
            elif car_direction == Direction.DOWN:
                # 无目标但方向向下 → 按距离降序（最近的先服务）
                self._items = sorted(cache, reverse=True)
            elif car_direction == Direction.UP:
                # 无目标但方向向上 → 按距离升序
                self._items = sorted(cache)
            else:
                self._items = sorted(cache)
        else:
            if car_direction == Direction.UP and target is not None:
                floors = sorted(cache)
                forward = [f for f in floors if pos < f <= target]
                backward = [f for f in floors if f <= pos or f > target]
                self._items = forward + backward
            elif car_direction == Direction.DOWN and target is not None:
                floors = sorted(cache)
                forward = [f for f in floors if pos > f >= target]
                backward = [f for f in floors if f >= pos or f < target]
                self._items = list(reversed(forward)) + list(reversed(backward))
            elif car_direction == Direction.DOWN:
                self._items = sorted(cache, reverse=True)
            elif car_direction == Direction.UP:
                self._items = sorted(cache)
            else:
                self._items = sorted(cache)

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
        # 未被派车的外召请求（无空闲车时暂存，等车空闲后自动派车）
        self._pending_hall_calls: set[tuple[int, str]] = set()

        for cid in app.car_ids:
            self._passenger_queue[cid] = PassengerQueue(
                mode=self._ui_config.get('passenger', {}).get('queue_mode', 'discard')
            )
            self._button_cache[cid] = set()
            self._pickup_active[cid] = {}
            self._flash_tasks[cid] = {}

        # /debug show ai_need_2: 受控的事件级 print 总开关（默认静默）
        # 归类：door_button / door_opened / door_closed / move_done
        self.ai_need_2_enabled: bool = False

    def _log_event(self, msg: str) -> None:
        """受 ai_need_2 开关控制的事件级 print（已废弃，保留兼容）"""
        if self.ai_need_2_enabled:
            sys.stderr.write(msg + '\n')
            sys.stderr.flush()

    def _log_stderr(self, msg: str) -> None:
        """写 stderr（已被 App 替换为 TeeStderr → 自动写文件+终端）"""
        sys.stderr.write(msg + '\n')
        sys.stderr.flush()

    def _reload_ui_config(self) -> None:
        try:
            with self._ui_config_path.open('r', encoding='utf-8') as f:
                self._ui_config = yaml.safe_load(f) or {}
        except (FileNotFoundError, yaml.YAMLError) as e:
            self._log_stderr(f'[pm] ui_config 加载失败: {e}，使用默认值')
            self._ui_config = {}

    # ===== config helpers =====

    def _door_close_delay(self) -> float:
        return self._ui_config.get('passenger', {}).get('door_close_delay', 10.0)

    def _light_off_delay(self) -> float:
        return self._ui_config.get('passenger', {}).get('light_off_delay', 600.0)

    def _human_presence_off_delay(self) -> float:
        # 新配置项 human_presence_off_delay 优先（门关 + 无请求后多久 = -1）
        # 未配置则复用老的 light_off_delay
        cfg = self._ui_config.get('passenger', {})
        return float(cfg.get('human_presence_off_delay',
                             cfg.get('light_off_delay', 600.0)))

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
            # 快捷路径：已有车在该层且门开着/正在关 → 亮灯 + 取消关门 + 必要时重开
            for cid in self._app.car_ids:
                car = self._app.cars[cid]
                if car.position != floor:
                    continue
                if car.door_state in (DoorState.OPEN, DoorState.OPENING):
                    # 门已开:检查方向冲突——若车正在上行(有上方待响应外呼/内召)
                    # 且外呼是 down@当前层,不应拦截(让 dispatch 派其他车),
                    # 否则同层反向乘客等死
                    has_pickup_above = any(
                        f > floor for (f, d), a in self._pickup_active[cid].items() if a)
                    has_pickup_below = any(
                        f < floor for (f, d), a in self._pickup_active[cid].items() if a)
                    if (direction == 'down' and has_pickup_above
                            and not has_pickup_below):
                        # 车要上行(只有上方请求),下行的同层外呼不拦截
                        pass
                    elif (direction == 'up' and has_pickup_below
                          and not has_pickup_above):
                        # 车要下行(只有下方请求),上行的同层外呼不拦截
                        pass
                    else:
                        # 方向无冲突→走快捷路径:只亮灯,不 cancel 关门 cron
                        self._pickup_active[cid][(floor, direction)] = True
                        self._app.cars[cid].last_dispatch_direction = (
                            Direction.UP if direction == 'up' else Direction.DOWN)
                        await self._ensure_lights_on(cid)
                        await self._app.set_hall_indicator(floor, direction, True)
                        self._log_stderr(f'[hall_call] {direction}@L{floor} → car{cid} door open, keep LED')
                        return
                elif car.door_state == DoorState.CLOSING:
                    # 门正在关 → 亮灯 + 取消关门 cron + 中断关门 + 重新开门
                    self._pickup_active[cid][(floor, direction)] = True
                    self._app.cars[cid].last_dispatch_direction = (
                        Direction.UP if direction == 'up' else Direction.DOWN)
                    await self._ensure_lights_on(cid)
                    await self._app.set_hall_indicator(floor, direction, True)
                    await self._app.cron.cancel(
                        self._close_door_job_name(cid))
                    # ★ 中断当前 CLOSE_DOOR 动作，让 executor 立即处理 OPEN_DOOR
                    self._app.executors[cid].door.cancel_for_reopen()
                    await self._app.action_queues[cid].put(
                        Action(ActionKind.OPEN_DOOR))
                    self._log_stderr(f'[hall_call] {direction}@L{floor} → car{cid} door closing, reopen')
                    return
            # 第一层防线：该 (floor, direction) 已被某部车服务 → 跳过
            for cid in self._app.car_ids:
                if self._pickup_active.get(cid, {}).get((floor, direction), False):
                    return
            target_cid = self._app._dispatch_hall_call(floor, direction)
            if target_cid is None:
                self._log_stderr(f'[hall_call] {direction}@L{floor} no available car, keeping LED + pending')
                # 仍然亮起外召指示灯
                await self._app.set_hall_indicator(floor, direction, True)
                # 记住这个待派请求
                self._pending_hall_calls.add((floor, direction))
                # 立即尝试用刚空闲的车派待派请求（不等到 _on_door_closed）
                for cid in self._app.car_ids:
                    car = self._app.cars[cid]
                    if (car.state == CarState.READY
                            and car.direction == Direction.IDLE
                            and car.door_state == DoorState.CLOSED
                            and car.position is not None):
                        # 有空闲车，尝试派一个待派外呼
                        await self._try_dispatch_pending_hall_calls(cid)
                        break
                # 诊断信息：列出每部车被过滤的原因,帮助现场排查
                # (例:门开着 / 手动模式 / 未初始化 / 位置未知)
                for cid in self._app.car_ids:
                    car = self._app.cars[cid]
                    if car.state != CarState.READY:
                        self._log_stderr(f'         · car{cid}: state={car.state.value}')
                    elif car.position is None:
                        self._log_stderr(f'         · car{cid}: position=None')
                    elif self._app.manual_mode.get(cid, False):
                        self._log_stderr(f'         · car{cid}: 手动模式中')
                    elif car.door_state != DoorState.CLOSED:
                        self._log_stderr(f'         · car{cid}: 门={car.door_state.value}')
                return
            # mark pickup, light indicator
            self._pickup_active[target_cid][(floor, direction)] = True
            # 记录派车方向（用于 compile 排序）
            self._app.cars[target_cid].last_dispatch_direction = (
                Direction.UP if direction == 'up' else Direction.DOWN)
            # 外召有人呼叫 → 被派车的轿厢 human_presence 至少 0
            await self._ensure_lights_on(target_cid)
            await self._app.set_hall_indicator(floor, direction, True)
            car = self._app.cars[target_cid]
            if car.position == floor:
                # ★ 同层外召→开门前亮起方向灯（不等 MOVE 启动，门开着时就告诉乘客去向）
                await self._app.executors[target_cid].motor.set_direction_indicator(direction)
                await self._app.action_queues[target_cid].put(
                    Action(ActionKind.OPEN_DOOR))
                self._log_stderr(f'[hall_call] {direction}@L{floor} → car{target_cid} at floor, opening')
            else:
                # 车不在当前层，尝试顺路改道（change_internal）
                car = self._app.cars[target_cid]
                old_target = car.target_floor
                changed = await self._app.change_internal(floor, car_id=target_cid)
                if changed == 'accepted':
                    # 顺路改道到外呼楼层，原目标保留
                    # ★ 必须设外召来源标记，否则到站后 _handle_algorithm_state_change
                    # 看到 origin='internal' 不会开门
                    self._app.pending_call_origin[target_cid][floor] = 'hall'
                    if old_target is not None and old_target != floor:
                        self._app.pending_calls[target_cid].append(old_target)
                    self._log_stderr(f'[hall_call] {direction}@L{floor} → car{target_cid} (顺路改道)')
                else:
                    # 距离不够或不顺路，加入 pending_calls 等返回
                    await self._app.call_internal(floor, car_id=target_cid, origin='hall')
                    # 派车成功 → 通过小脑立即亮起预测方向（不等 MOVE 动作启动）
                    await self._app.set_predicted_direction_indicator(target_cid, floor)
                    self._log_stderr(f'[hall_call] {direction}@L{floor} → car{target_cid}')
        else:
            # bit=0: 按钮松开 → 判断车是否已到站
            for cid in self._app.car_ids:
                if self._pickup_active.get(cid, {}).get((floor, direction), False):
                    car = self._app.cars[cid]
                    if car.position == floor:
                        # 车已到站 → 熄灯 + 停闪烁 + 启动关门 cron
                        await self._app.set_hall_indicator(floor, direction, False)
                        flash_key = f'{direction}_{floor}'
                        task = self._flash_tasks[cid].pop(flash_key, None)
                        if task is not None and not task.done():
                            task.cancel()
                        await self._start_close_door_cron(cid, floor, direction)
                    else:
                        # 车还没到 → 保持亮灯
                        await self._app.set_hall_indicator(floor, direction, True)
                    return

    async def on_cabin_button(self, cid: int, floor: int) -> None:
        """cabin button: door open/opening/closing → cache; door closed → try change_internal first, fallback to call_internal"""
        car = self._app.cars[cid]
        if car.door_state in (DoorState.OPEN, DoorState.OPENING, DoorState.CLOSING):
            # 门未关好时只缓存，不中断关门（内召不需要重开门）
            self._button_cache[cid].add(floor)
        else:
            # 门已关：先尝试缩短当前行程（中途加站），失败再作为新请求入队
            old_target = car.target_floor
            result = await self._app.change_internal(floor, car_id=cid)
            if result == 'accepted':
                # change_internal 已设 target_floor=floor 并清空 pending_calls
                # 但原目标楼层（如 L10）需要保留，不能丢弃
                pq = self._passenger_queue[cid]
                remaining = [floor]
                if old_target is not None and old_target != floor:
                    self._app.pending_calls[cid].append(old_target)
                    remaining.append(old_target)
                pq.clear()
                pq._items = remaining
                return
            elif result == 'rejected':
                # 刹不住车或方向不符，作为新内呼入队（等当前行程完成后再去）
                await self._app.call_internal(floor, car_id=cid)
                return
            # result == 'not_running' → 车没在移动，作为普通内呼
            await self._app.call_internal(floor, car_id=cid)
            # 派车成功 → 通过小脑立即亮起预测方向（不等 MOVE 动作启动）
            await self._app.set_predicted_direction_indicator(cid, floor)

    async def on_door_button(self, cid: int, signal: str,
                              bit: int = 1) -> None:
        """door button: open/close with hold/release semantics"""
        car = self._app.cars[cid]
        self._log_event(f'[door_button] car{cid} {signal} bit={bit} door={car.door_state.value}')
        if signal == 'door_open_button':
            if bit == 1:
                # 按下: 取消关门 cron
                await self._app.cron.cancel(self._close_door_job_name(cid))
                if car.door_state == DoorState.CLOSING:
                    # ★ 门正在关 → 中断关门 + 重开（与外召/内召 reopen 同机制）
                    self._app.executors[cid].door.cancel_for_reopen()
                    await self._app.action_queues[cid].put(
                        Action(ActionKind.OPEN_DOOR))
                    self._log_event(f'[door_button] car{cid} open: door closing, cancel_for_reopen + reopen')
                elif car.door_state == DoorState.CLOSED:
                    await self._app.action_queues[cid].put(
                        Action(ActionKind.OPEN_DOOR))
            else:
                # 松开: 门开着/正在开 → 启关门 cron
                if car.door_state in (DoorState.OPEN, DoorState.OPENING):
                    await self._schedule_close_door_cron_job(
                        cid, self._close_door_job_name(cid))
        elif signal == 'door_close_button':
            if bit == 1:
                # 按下: 取消关门 cron + 检查外召/开门按钮后决定是否关门
                await self._app.cron.cancel(self._close_door_job_name(cid))
                # ★ 电梯移动中不允许关门（防止 CLOSE_DOOR 抢占正在执行的 MOVE，
                # 导致 current_action 被覆盖、计数器崩溃）
                if car.direction != Direction.IDLE:
                    self._log_event(f'[door_button] car{cid} close ignored: car moving {car.direction.value}')
                    return
                if car.door_state == DoorState.OPEN:
                    # ★ 只在门完全打开后才响应关门，开门中(OPENING)不响应
                    # ★ 同时检查外召按钮 + 开门按钮,任何一个按住就不关
                    hall_held = self._is_any_hall_button_held(cid)
                    open_held = self._is_open_button_held(cid)
                    self._log_event(f'[door_button] car{cid} close: door={car.door_state.value}, '
                          f'hall_held={hall_held}, open_held={open_held}, '
                          f'cache={sorted(self._button_cache[cid])}')
                    if not hall_held and not open_held:
                        await self._app.action_queues[cid].put(
                            Action(ActionKind.CLOSE_DOOR))
                    else:
                        held = []
                        if hall_held:
                            held.append('hall')
                        if open_held:
                            held.append('open')
                        self._log_event(f'[door_button] car{cid} close ignored: '
                              f'{"+".join(held)} button still held')
            # bit=0 松开: 关门动作已发出,no-op

    async def _ensure_lights_on(self, car_id: int) -> None:
        """如果人可能在场(human_presence != -1),开灯开风扇

        IO 写操作（set_light/set_fan）用 try/except 保护：
        即使开灯失败（如 HTTP 超时），human_presence 状态也必须正确设置，
        不能让异常传播出去打断外召派车/开门链路。
        """
        car = self._app.cars[car_id]
        prev = car.human_presence
        car.human_presence = max(0, prev)
        if prev <= 0:
            try:
                await self._app.ui[car_id].set_light(True)
            except Exception:
                pass
            try:
                await self._app.ui[car_id].set_fan(True)
            except Exception:
                pass

    def _is_open_button_held(self, car_id: int) -> bool:
        """检查开门按钮是否被按住(读 IO cache)"""
        return self._app.is_door_open_button_held(car_id)

    def _is_any_hall_button_held(self, car_id: int) -> bool:
        """检查当前楼层是否有外召按钮仍被按住（读 IO cache）

        用于关门保护：只要外召按钮仍按住，门就不应该关。
        """
        car = self._app.cars[car_id]
        pos = car.position
        if pos is None:
            return False

        for (floor, direction), active in self._pickup_active[car_id].items():
            if not active or floor != pos:
                continue
            if self._app.is_hall_button_held(pos, direction):
                return True
        return False

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
        """MOVE 到站 → 标记服务完成 + 开门让乘客上下

        用 car.position 与乘客队列匹配，不依赖 car.target_floor
        （因为 _handle_algorithm_state_change 会先清 target_floor）。

        Bug 修复:之前只当 pq 还有剩余时才开门,导致单次内召到站
        （pq=[5] → serve 5 → pq 空 → 不开门）乘客被困。
        现在只要当前层是被服务过的(在 pq 里),就开门让乘客下梯。
        """
        car = self._app.cars[car_id]
        pq = self._passenger_queue[car_id]
        if pq:
            pos = car.position
            if pos is not None and pos in set(pq.items):
                pq.mark_served(pos)
                self._log_event(f'[move_done] car{car_id} served L{pos}, remaining pq={pq.items}')
                # 开门让乘客下梯:不管是单次还是多次内召,只要本层被服务过
                await self._app.action_queues[car_id].put(
                    Action(ActionKind.OPEN_DOOR))
        # 尝试处理待派外召（不在这里：到站途中来的外召等下站 _on_door_closed 再派）
        # _try_dispatch_pending_hall_calls 会污染 car.last_dispatch_direction，导致
        # _on_door_opened 方向检查误判 + _on_door_closed effective_dir 偏航 → 路线丢失

    async def _on_door_opened(self, car_id: int) -> None:
        """door opened → 检查外召按钮状态 + 条件性熄灯/启动关门 cron

        关门逻辑:
          - 开门时检查对应外召按钮是否仍按住（读 IO cache）
          - 仍按住：不熄灯 + 不启动关门 cron（等松手再处理）
          - 已松开：熄灯 + 启动关门 cron
          - 松手时另由 on_hall_call bit=0 handler 统一处理 LED

        human_presence 转移:
          - 有外召 pickup（接客开门）：-1 → 0（可能上客）
          - 无 pickup（到站送客开门）：1 → 0（可能下客）
        """
        app = self._app
        car = app.cars[car_id]
        # human_presence 状态转移（开门时）
        has_pickup = any(self._pickup_active[car_id].values())
        if has_pickup:
            # 接客开门：-1 → 0（有人可能上客），开灯开风扇
            await self._ensure_lights_on(car_id)
        else:
            # 到站送客开门：1 → 0（人可能下客离开）
            if car.human_presence == 1:
                car.human_presence = 0

        # ---- 开门时设置方向灯 ----
        # 规则：门开→显示响应的外呼方向；运行时→由 _start_move_up/down 设置继电器方向
        active_pickups = [(f, d) for (f, d), active in
                          self._pickup_active[car_id].items() if active]
        if active_pickups:
            # 优先用当前开门楼层的 pickup 方向
            door_dir: str | None = None
            for f, d in active_pickups:
                if f == car.position:
                    door_dir = d
                    break
            if door_dir is None and car.position is not None:
                # 当前层无 pickup（如还有下一站），从外召整体方向推断
                above = any(f > car.position for f, _ in active_pickups)
                below = any(f < car.position for f, _ in active_pickups)
                if above and not below:
                    door_dir = 'up'
                elif below and not above:
                    door_dir = 'down'
            if door_dir is not None:
                await app.executors[car_id].motor.set_direction_indicator(door_dir)
        else:
            # 无外召 pickup（内召到站/纯开门）→ 灭方向灯
            await app.executors[car_id].motor.set_direction_indicator(None)


        held_pickups: list[tuple[int, str]] = []     # 按钮仍按住
        released_pickups: list[tuple[int, str]] = []  # 按钮已松开

        for (floor, direction), active in list(
                self._pickup_active[car_id].items()):
            if not active:
                continue
            # 只处理当前开门楼层的 pickup（读物理 floor_door_lock 信号，不读 car.position）
            # 外召 LED 应和本层门事件绑定，不应在别的楼层开门时被熄灭
            door_open_at_floor = app.is_floor_door_open(car_id, floor)
            if not door_open_at_floor:
                continue
            # 读 IO cache 检查按钮是否仍按住
            button_held = app.is_hall_button_held(floor, direction)

            if button_held:
                held_pickups.append((floor, direction))
            else:
                # 车到站、按钮已松 → 熄灯
                await app.set_hall_indicator(floor, direction, False)
                released_pickups.append((floor, direction))

        # 注意：不在这里清空 button_cache！
        # 轿内按钮可能在 door_open_done 之前到达（IO 事件顺序不确定），
        # 提前清空会丢失请求。button_cache 由 _on_door_closed 在合并后清空。

        # 到站开门 → 熄灭当前楼层的轿内按钮 LED
        pos = car.position
        if pos is not None:
            await app.ui[car_id].set_cabin_button_led(pos, False)

        # 如果这是本次行程终点（乘客队列已空），统一熄灭所有残留轿内灯
        # 中途非法内呼（如刹不住车）产生的 LED 在此清理
        pq = self._passenger_queue[car_id]
        if not pq:
            building = self._app.config.get('building', {})
            min_f = building.get('min_floor', 1)
            max_f = building.get('max_floor', 10)
            # 跳过本次开门期间新按的按钮（保留给下一趟）
            skip = self._button_cache.get(car_id, set())
            for f in range(min_f, max_f + 1):
                if f not in skip:
                    await app.ui[car_id].set_cabin_button_led(f, False)
            self._log_event(f'[door_opened] car{car_id} terminal stop, cleared all cabin LEDs (skip={sorted(skip)})')

        # 清除已释放的 pickup（允许再次按下时重新触发）
        for (floor, direction) in released_pickups:
            self._pickup_active[car_id].pop((floor, direction), None)

        if not held_pickups:
            # 没有按住的 pickup → 启动关门 cron
            self._log_event(f'[door_opened] car{car_id} scheduling close cron (released={released_pickups})')
            await self._schedule_close_door_cron_job(
                car_id, self._close_door_job_name(car_id),
                hall_signals=released_pickups if released_pickups else None)
        # held_pickups 非空时：不启动 cron，等 on_hall_call bit=0 松手后再启动

    async def _on_door_closed(self, car_id: int) -> None:
        """门已关闭 → 清接客状态、合并队列 → 出发或熄灯

        如果关门被 cancel_for_reopen() 取消（door_state 仍为 CLOSING），
        跳过处理——后续 OPEN_DOOR 会无缝接管。
        """
        car = self._app.cars[car_id]
        # 取消守卫：关门被中断（door_state 未被 executor 设为 CLOSED）
        if car.door_state != DoorState.CLOSED:
            return
        # 只清除本楼层已服务的 pickup，未来站点的 pickup 保留
        pos = car.position
        if pos is not None:
            for key in list(self._pickup_active[car_id]):
                if key[0] == pos:
                    del self._pickup_active[car_id][key]

        pq = self._passenger_queue[car_id]

        # 合并已有队列余项 + 本次开门期间的新内召缓存
        all_requests = set(pq.items) | self._button_cache[car_id]
        pos = car.position

        # 先计算车的行驶方向（不依赖 pending hall calls，避免反方向混入）
        effective_dir = car.direction
        if effective_dir == Direction.IDLE:
            if car.last_dispatch_direction != Direction.IDLE:
                effective_dir = car.last_dispatch_direction
            elif all_requests and car.position is not None:
                above = any(f > car.position for f in all_requests)
                below = any(f < car.position for f in all_requests)
                if above and not below:
                    effective_dir = Direction.UP
                elif below and not above:
                    effective_dir = Direction.DOWN

        # 如果仍是 IDLE（无内召无历史方向），从 pending 外呼位置推断方向
        sweep_mode = False
        if effective_dir == Direction.IDLE and self._pending_hall_calls and car.position is not None:
            above = sum(1 for f, _ in self._pending_hall_calls if f > car.position)
            below = sum(1 for f, _ in self._pending_hall_calls if f < car.position)
            if above > below:
                effective_dir = Direction.UP   # 全在上方 → 向上 sweep
                sweep_mode = True
            elif below > above:
                effective_dir = Direction.DOWN  # 全在下方 → 向下 sweep
                sweep_mode = True
            else:
                # 上方下方等量 → 不推断方向，留给 _try_dispatch_pending_hall_calls
                pass

        # 将顺路 pending 外召也编入路线
        if effective_dir != Direction.IDLE:
            if sweep_mode:
                # 扫路模式：只编入最远站，中间站保留等回程
                candidates = [(f, d) for f, d in self._pending_hall_calls
                              if (f > pos if effective_dir == Direction.UP else f < pos)]
                if candidates:
                    key = max(candidates) if effective_dir == Direction.UP else min(candidates)
                    floor, direction = key
                    all_requests.add(floor)
                    self._pending_hall_calls.discard((floor, direction))
                    self._pickup_active[car_id][(floor, direction)] = True
            else:
                for (floor, direction) in list(self._pending_hall_calls):
                    add = (direction == 'up' and effective_dir == Direction.UP and floor > pos) or \
                          (direction == 'down' and effective_dir == Direction.DOWN and floor < pos)
                    if add:
                        all_requests.add(floor)
                        self._pending_hall_calls.discard((floor, direction))
                        self._pickup_active[car_id][(floor, direction)] = True
        self._log_event(f'[door_closed] car{car_id} cache={sorted(self._button_cache[car_id])}, pq={pq.items}, merged={sorted(all_requests)}, dir={effective_dir.value}')
        pq.compile(
            cache=all_requests,
            car_position=car.position,
            car_direction=effective_dir,
            current_target=car.target_floor,
        )
        self._button_cache[car_id].clear()

        if pq:
            pos = car.position
            # 跳过当前楼层：已经在本站服务过，无须再 dispatch
            if pos is not None and pq.next_target() == pos:
                pq.mark_served(pos)

            first = pq.next_target()
            if first is not None:
                self._log_event(f'[door_closed] car{car_id} dispatching → L{first}')
                added = await self._app.call_internal(first, car_id=car_id)
                if not added:
                    # call_internal 拒绝（如 floor 已在 pending_calls）
                    # 需要手动 tick 让算法继续处理剩余任务
                    if self._app.executors[car_id].current_action is None:
                        await self._app._tick(car_id)
                return

        # 走到这里说明 pq 已空（或全部已 serve），但 pending_calls 可能还有剩余任务
        #（如中途 cabin button 加站后原目标已到，新站还在 pending_calls 里）
        pending = self._app.pending_calls.get(car_id, [])
        if pending and self._app.executors[car_id].current_action is None:
            self._log_event(f'[door_closed] car{car_id} pending_calls={pending}, fallback tick')
            await self._app._tick(car_id)
        else:
            self._log_event(f'[door_closed] car{car_id} empty, lights off cron')
            await self._start_lights_off_cron(car_id)

        # 尝试处理待派的外召请求（之前无空闲车的）
        await self._try_dispatch_pending_hall_calls(car_id)

    async def _try_dispatch_pending_hall_calls(self, car_id: int) -> None:
        """检查待派外召集合，尝试用当前空闲车派车"""
        if not self._pending_hall_calls:
            return
        car = self._app.cars[car_id]
        # 只用车空闲（IDLE + CLOSED + READY）的车来派
        if (car.state != CarState.READY
                or car.direction != Direction.IDLE
                or car.door_state != DoorState.CLOSED
                or car.position is None):
            return

        dispatched: list[tuple[int, str]] = []
        for (floor, direction) in list(self._pending_hall_calls):
            # 尝试派这辆车
            target_cid = self._app._dispatch_hall_call(floor, direction)
            if target_cid is not None:
                dispatched.append((floor, direction))
                self._pickup_active[target_cid][(floor, direction)] = True
                self._app.cars[target_cid].last_dispatch_direction = (
                    Direction.UP if direction == 'up' else Direction.DOWN)
                await self._ensure_lights_on(target_cid)
                await self._app.set_hall_indicator(floor, direction, True)
                target_car = self._app.cars[target_cid]
                if target_car.position == floor:
                    # ★ 待派同层外召→开门前亮起方向灯
                    await self._app.executors[target_cid].motor.set_direction_indicator(direction)
                    await self._app.action_queues[target_cid].put(
                        Action(ActionKind.OPEN_DOOR))
                    self._log_stderr(f'[hall_call] pending {direction}@L{floor} → car{target_cid} at floor, opening')
                else:
                    await self._app.call_internal(floor, car_id=target_cid, origin='hall')
                    self._log_stderr(f'[hall_call] pending {direction}@L{floor} → car{target_cid}')
                break  # 只派一个，避免同一 tick 连推多个 MOVE

        for item in dispatched:
            self._pending_hall_calls.discard(item)

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
            # door_close_button 不取消 cron：手动关门后 cron 触发时会自然退出
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
            # ★ 重量检查:状态 2 期间不调度关门(避免开门→关门 cron→立即再开循环)
            if car.weight_state == 2:
                return
            # ★ 检查外召按钮是否仍按住：按住则不关门，等松手后由 on_hall_call bit=0 重新调度
            if self._is_any_hall_button_held(car_id):
                self._log_stderr(f'[cron] car{car_id} close aborted: hall button still held')
                return  # 不调度关门，等 on_hall_call bit=0 handler 重新调度 cron
            # check light curtain before closing: if still blocked, reschedule
            if self._app.is_light_curtain_active(car_id):
                await self._app.cron.cancel(job_name)
                await self._schedule_close_door_cron_job(
                    car_id, job_name, floor, direction,
                    hall_signals=hall_signals)
                return
            await self._app.action_queues[car_id].put(
                    Action(ActionKind.CLOSE_DOOR))

        await self._app.cron.schedule(CronJob(
            name=job_name,
            trigger_time=time.monotonic() + delay,
            delay=delay,
            action=_close_door_action,
            event_rules=event_rules,
        ))

    # ===== 熄灯节能 + human_presence -1 确认 cron =====
    #
    # 双重角色：
    #   1. human_presence 三态机的 -1 确认（4 点是门关后状态计时 → 转 -1）
    #   2. 车灯节能（活人能取消，未人者占空间也关）
    # 两个语义合并：一旦事件取消，仍按 0 走到 -1，同时保留灯亮着（human_presence 0 还是表示不确定，灯不强制关）

    async def _start_lights_off_cron(self, car_id: int) -> None:
        """门已关 + 无 pending 请求 → 启动 10min cron

        到期后：human_presence 0 → -1（确认无人），同时关灯节能。
        任何开门/按钮/召唤触发均取消。
        """
        jn = self._lights_off_job_name(car_id)
        await self._app.cron.cancel(jn)
        delay = self._human_presence_off_delay()

        async def _lights_off_action():
            car = self._app.cars[car_id]
            if car.human_presence == 0:
                car.human_presence = -1
            try:
                await self._app.ui[car_id].set_light(False)
            except Exception:
                pass
            try:
                await self._app.ui[car_id].set_fan(False)
            except Exception:
                pass

        event_rules = [
            EventRule('door_open_button', car_id, 'cancel', 0),
            # door_close_button 不取消 cron：手动关门后 cron 触发时会自然退出
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
                self._log_stderr(f'[passenger] flash_loop car{car_id} 异常: {exc!r}')

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
