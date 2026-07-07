"""
app.py —— 异步装配与主循环

职责:
    - 加载所有 config
    - 装配多轿厢（6 部）：每部拥有独立的 Car / Executor / ActionQueue / VirtualPLC
    - 共享 IOClient / IOMapper / DisplayEncoder / Algorithm
    - IO 事件按 car_id 路由到对应的 executor
    - 暴露高层 API 给 console 调用（call / reset / status / etc.）
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import yaml

from .actions import Action, ActionKind, ActionQueue
from .algorithm import ElevatorAlgorithm, get_algorithm
from .cron import Cron, CronJob, EventRule
from .display import DisplayEncoder
from .executor import ActionExecutor
from .io_client import IOClient, IOEvent
from .io_mapper import IOMapper
from .player import Car, CarState, Direction, DoorState, IndicatorState
from .ui import UiController
from .virtual_plc import VirtualPLC

# 默认轿厢范围(若 config.yaml 里 elevator.car_ids 未配置)
DEFAULT_CAR_IDS = [1, 2, 3, 4, 5, 6]


class App:
    def __init__(
        self,
        config_path: str | Path,
        io_config_path: str | Path,
        display_config_path: str | Path,
        simulate: bool = False,
    ) -> None:
        self.config_path = Path(config_path)
        self.io_config_path = Path(io_config_path)
        self.display_config_path = Path(display_config_path)
        self.simulate = simulate

        self.config: dict[str, Any] = {}
        self._load_config()

        # ===== 共享组件 =====
        io2http = self.config['io2http']
        self.io = IOClient(
            http_url=io2http['http_url'],
            ws_url=io2http['ws_url'],
            simulate=simulate,
            debug=False,
            tick_interval_ms=io2http.get('tick_interval_ms', 100),
        )
        self.mapper = IOMapper(io_config_path)
        self.algorithm: ElevatorAlgorithm = get_algorithm(
            self.config['algorithm']['name']
        )

        # ===== 多轿厢 =====
        self.current_car_id: int = 1
        self.cars: dict[int, Car] = {}
        self.executors: dict[int, ActionExecutor] = {}
        self.action_queues: dict[int, ActionQueue] = {}
        self.pending_calls: dict[int, list[int]] = {}
        self.manual_mode: dict[int, bool] = {}
        self._executor_tasks: dict[int, asyncio.Task] = {}

        building = self.config['building']
        # 从 config 读 car_ids,默认 [1..6]
        # 注意:car_ids 在启动时确定,/reload 不会动态增删车
        self.car_ids: list[int] = list(
            self.config.get('elevator', {}).get('car_ids', DEFAULT_CAR_IDS)
        )

        # 关键:每部电梯独立的 io_write(写通道),避免 6 部车共享一个
        # write_buffer + 一次 tick flush 出 30+ 个地址,S7 read-modify-write
        # 顺序就是车号顺序,各车接触器实际建立时间错开("偏了但没偏太多")。
        # 共享 self.io 的 input/output cache(读)让"只写"实例也能看到最新 IO 状态。
        io2http_cfg = self.config['io2http']
        self._shared_caches = {
            'input': self.io._input_cache,
            'output': self.io._output_cache,
        }
        self.io_write: dict[int, IOClient] = {}
        for cid in self.car_ids:
            self.io_write[cid] = IOClient(
                http_url=io2http_cfg['http_url'],
                ws_url=None,  # 不连 WS,bitmap 由 self.io 负责
                alias=io2http_cfg.get('alias', 'plc'),
                simulate=simulate,
                debug=False,
                tick_interval_ms=io2http_cfg.get('tick_interval_ms', 100),
                shared_input_cache=self._shared_caches,
            )

        # display 也用 per-car io,避免 6 部车同时更新显示也拥堵
        # 但 display 写入不紧急(异步 tick 合并即可),共享 self.io 也行
        # 这里为了简单,display 仍用 self.io(只影响 tick 自动 flush,不拥堵 critical 操作)
        self.display = DisplayEncoder(display_config_path, io=self.io, mapper=self.mapper)

        for cid in self.car_ids:
            self.cars[cid] = Car(car_id=cid)
            self.action_queues[cid] = ActionQueue()
            self.pending_calls[cid] = []
            self.manual_mode[cid] = False
            self.executors[cid] = ActionExecutor(
                car=self.cars[cid],
                io=self.io,
                io_write=self.io_write[cid],
                mapper=self.mapper,
                display=self.display,
                car_id=cid,
                init_direction=self.config['elevator']['initialization_direction'],
                top_base_floor=building['top_base_floor'],
                bottom_base_floor=building['bottom_base_floor'],
                on_action_done=self._make_on_action_done(cid),
                on_emergency_stop=self._make_on_emergency_stop(cid),
                station_seek_enabled=self.config['elevator'].get('station_seek', False),
                action_queue=self.action_queues[cid],
            )

        # 装配 per-car UiController(与 executors 平级,游戏 entity-component 模式)
        # UI 是电梯实体的属性,通过 app.ui[cid].set_xxx() 写,car.ui.xxx 读
        self.ui: dict[int, UiController] = {}
        for cid in self.car_ids:
            self.ui[cid] = UiController(
                io_write=self.io_write[cid],
                mapper=self.mapper,
                car_id=cid,
                car=self.cars[cid],
            )

        # Hall indicator 是建筑级信号(car_id=0),不属于任何轿厢
        # 状态单独存在 App 上(不挂在 Car 上)
        self._hall_indicator_state: dict[tuple[int, str], bool] = {}

        # 同轿厢互斥锁(/door 同车不能并发)
        # 用 bool 标志即可:asyncio 是协作式调度,await done_event.wait()
        # 期间事件循环可调度其他 coroutine,但其他 /door 调用会看到 busy=True 而退出。
        self._door_busy: dict[int, bool] = {cid: False for cid in self.car_ids}

        self._executor_task: asyncio.Task | None = None
        self.debug = False
        self._usermode = False
        self.cron = Cron()
        self.pending_call_origin: dict[int, dict[int, str]] = {}
        for cid in self.car_ids:
            self.pending_call_origin[cid] = {}

    @property
    def car(self) -> Car:
        """当前选中的轿厢（console 兼容）"""
        return self.cars[self.current_car_id]

    @property
    def executor(self) -> ActionExecutor:
        """当前选中的 executor（console 兼容）"""
        return self.executors[self.current_car_id]

    @property
    def action_queue(self) -> ActionQueue:
        """当前选中的 action queue（console 兼容）"""
        return self.action_queues[self.current_car_id]

    def _load_config(self) -> None:
        with self.config_path.open('r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

    # ===== 生命周期 =====

    async def start(self) -> None:
        await self.io.start()
        if hasattr(self.io, 'set_known_i_addresses'):
            self.io.set_known_i_addresses(set(self.mapper.lookup_all_i_addresses()))
        self.io.add_listener(self._on_io_event)
        self.io.add_listener(self._on_hall_call_event)
        self.io.add_listener(self._on_cabin_button_event)
        self.cron.register(self.io, self.mapper)
        await self.cron.start()
        # 起 6 部电梯各自的写 IOClient(共享 input_cache,各自独立 flush)
        for io_w in self.io_write.values():
            await io_w.start()

        building = self.config.get('building', {})
        for cid in self.car_ids:
            task = asyncio.create_task(
                self.executors[cid].run_loop(self.action_queues[cid])
            )
            self._executor_tasks[cid] = task

        if self.simulate:
            self.virtual_plcs: dict[int, VirtualPLC] = {}
            for cid in self.car_ids:
                vplc = VirtualPLC(
                    io=self.io,
                    mapper=self.mapper,
                    car=self.cars[cid],
                    car_id=cid,
                    top_base=building.get('top_base_floor', 11),
                    bottom_base=building.get('bottom_base_floor', 0),
                    top_floor=building.get('max_floor', 10),
                    bottom_floor=building.get('min_floor', 1),
                )
                self.virtual_plcs[cid] = vplc
                vplc.start()
            print(f'[vplc] 已启动 {len(self.car_ids)} 部虚拟 PLC')

    async def stop(self) -> None:
        await self.cron.stop()
        if self.simulate:
            for vplc in getattr(self, 'virtual_plcs', {}).values():
                await vplc.stop()
        for task in self._executor_tasks.values():
            if task and not task.done():
                task.cancel()
        for io_w in self.io_write.values():
            await io_w.stop()
        await self.io.stop()

    # ===== IO 事件路由（按 car_id） =====

    async def _on_io_event(self, event: IOEvent) -> None:
        """IO 变化事件 → 查找归属轿厢 → 交给对应 executor"""
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        cid = sig[0] if sig and sig[0] else self.current_car_id
        if cid in self.executors:
            await self.executors[cid].on_io_event(event)

    async def _on_hall_call_event(self, event: IOEvent) -> None:
        """IO 监听器: hall_call_up_X / hall_call_down_X 上升沿 → 派车

        只在 usermode 启用时响应（外召按钮按下 = 客人按 = 系统接客）。
        按下 (bit=1) 派车；松开门 (bit=0) 忽略——外召是锁存的，
        由 PLC/算法完成召唤后自行熄灭指示灯。
        """
        if not self._usermode:
            return
        if event.bit != 1:
            return

        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None:
            return
        car_id, signal_name = sig
        if car_id != 0:
            return  # hall_call 是全局信号（car_id=0）

        direction: str | None = None
        floor: int | None = None
        if signal_name.startswith('hall_call_up_'):
            direction = 'up'
            try:
                floor = int(signal_name[len('hall_call_up_'):])
            except ValueError:
                return
        elif signal_name.startswith('hall_call_down_'):
            direction = 'down'
            try:
                floor = int(signal_name[len('hall_call_down_'):])
            except ValueError:
                return
        else:
            return

        target_cid = self._dispatch_hall_call(floor, direction)
        if target_cid is None:
            print(f'[hall_call] {direction}@L{floor} 无可用轿厢')
            return

        await self.call_internal(floor, car_id=target_cid, origin='hall')
        print(f'[hall_call] {direction}@L{floor} → car{target_cid}')

    def _dispatch_hall_call(self, floor: int, direction: str) -> int | None:
        """派车算法：顺向优先 + 空闲最近

        优先级：
            0. 顺向经过（car moving dir == call dir，且 position → target_floor 之间会经过 floor）
            1. 空闲（direction == IDLE，无当前任务）
            其他：跳过（方向相反 / 同向但 target 已过 floor / 在忙）

        同优先级按距离升序；距离相同取小 car_id。

        Returns:
            选中的 car_id，或 None（无可用轿厢）
        """
        candidates: list[tuple[int, int, int]] = []  # (priority, distance, car_id)

        for cid in self.car_ids:
            car = self.cars[cid]

            if car.state != CarState.READY or car.position is None:
                continue
            if self.manual_mode.get(cid, False):
                continue

            pos = car.position
            moving_dir = car.direction
            target = car.target_floor

            # 顺向且会经过该层
            same_dir_pass = False
            if direction == 'up' and moving_dir == Direction.UP and target is not None:
                if pos < floor <= target:
                    same_dir_pass = True
            elif direction == 'down' and moving_dir == Direction.DOWN and target is not None:
                if pos > floor >= target:
                    same_dir_pass = True

            if same_dir_pass:
                candidates.append((0, abs(floor - pos), cid))
            elif moving_dir == Direction.IDLE:
                candidates.append((1, abs(floor - pos), cid))

        if not candidates:
            return None

        candidates.sort()
        return candidates[0][2]

    async def _on_cabin_button_event(self, event: IOEvent) -> None:
        """IO 监听器: cabin_button_X 上升沿 → 内召 + human_presence

        只在 usermode 启用时响应。
        按下 (bit=1) 认为有人在轿厢内，自毁熄灯 cron。
        """
        if not self._usermode:
            return
        if event.bit != 1:
            return

        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None:
            return
        cid, signal_name = sig
        if cid == 0 or cid not in self.car_ids:
            return
        if not signal_name.startswith('cabin_button_'):
            return

        try:
            floor = int(signal_name[len('cabin_button_'):])
        except ValueError:
            return

        # 有人
        car = self.cars[cid]
        car.human_presence = 1

        # 自毁熄灯 cron
        await self.cron.cancel(f'car{cid}_lights_off')

        # 内召
        await self.call_internal(floor, car_id=cid)

    # ===== 协调（按轿厢） =====

    async def _tick(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        if self.debug:
            print(f'[tick] car={cid} pos={self.cars[cid].position} '
                  f'pending={self.pending_calls[cid]}')
        actions = self.algorithm.decide(self.cars[cid], self.pending_calls[cid])
        for action in actions:
            # 推 MOVE 时如果 target_floor 还没设,从 pending[0] 取(FIFO)
            if action.kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN) and self.cars[cid].target_floor is None:
                if self.pending_calls[cid]:
                    self.cars[cid].target_floor = self.pending_calls[cid][0]
            if self.debug:
                print(f'[tick]   → {action}')
            await self.action_queues[cid].put(action)

    def _make_on_action_done(self, car_id: int):
        async def _on_action_done(last_action: Action) -> None:
            await self._on_action_done(car_id, last_action)
        return _on_action_done

    async def _on_action_done(self, car_id: int, last_action: Action) -> None:
        """
        Action 完成事件分发器

        只做算法编排 (mid-level):
        - MOVE 完成 → 清 pending, 外召到站开门
        - INITIALIZE 完成 → 启动方向运行
        上层应用逻辑（开关门后自动流程等）通过事件监听机制由外部模块接入。
        """
        if last_action is None:
            await self._tick(car_id)
            return

        advanced = await self._handle_algorithm_state_change(car_id, last_action)
        if not advanced:
            await self._tick(car_id)

    async def _handle_algorithm_state_change(
        self, car_id: int, last_action: Action
    ) -> bool:
        """
        算法状态转换 (mid-level)

        只关心"算法编排后的电梯下一步该做什么":
        - MOVE 完成 → 清 pending + 外召开门
        - INITIALIZE 完成 → 启动方向运行
        返回 True 表示已接管下一步动作(避免上层 _tick 再插一手)。
        """
        kind = last_action.kind

        if kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            pos = self.cars[car_id].position
            target = self.cars[car_id].target_floor
            if target is not None and pos == target:
                self.pending_calls[car_id] = [
                    c for c in self.pending_calls[car_id] if c != target
                ]
                origin = self.pending_call_origin[car_id].pop(target, 'internal')
                self.cars[car_id].target_floor = None
                # 外召到站 → 开门(内召不碰门)
                if origin == 'hall':
                    await self.action_queues[car_id].put(
                        Action(ActionKind.OPEN_DOOR))
                    return True
            return False

        if kind == ActionKind.INITIALIZE:
            target = last_action.floor
            if target is not None and target != self.cars[car_id].position:
                self.cars[car_id].target_floor = target
                dir_action = Action(
                    ActionKind.MOVE_UP if target > self.cars[car_id].position
                    else ActionKind.MOVE_DOWN
                )
                await self.action_queues[car_id].put(dir_action)
                return True
            return False

        return False

    def _make_on_emergency_stop(self, car_id: int):
        async def on_emergency():
            self.pending_calls[car_id].clear()
            self.cars[car_id].target_floor = None
            self.manual_mode[car_id] = False
            print(f'[emergency] car {car_id} 紧急停止')
        return on_emergency

    # ===== 高层 API（给 console 用） =====

    async def call_internal(self, floor: int, car_id: int | None = None,
                            origin: str = 'internal') -> None:
        cid = car_id if car_id is not None else self.current_car_id
        if floor in self.pending_calls[cid]:
            return
        # 车空闲时已在目标层 → 不残留 stale 条目（否则 call 当前层再 call 别层
        # 会留下 pending=[当前层],到达别层后被 algoritm 拉回去）
        # 注意:车有未完成召唤时（pending 非空）即使 pos==floor 也要记录,
        # 否则车移动中经过目标层时 call 会被静默丢弃。
        if self.cars[cid].position == floor and not self.pending_calls[cid]:
            return
        self.pending_calls[cid].append(floor)
        self.pending_call_origin[cid][floor] = origin
        # 只有空闲时才立即设目标（否则等当前任务完成后再从 pending[0] 取）
        if self.cars[cid].target_floor is None:
            self.cars[cid].target_floor = floor
        await self._tick(cid)

    async def change_internal(self, floor: int, car_id: int) -> str:
        """中途更改目的地

        MOVE_UP/MOVE_DOWN 运行时，将目标改为一个更近的楼层（缩短行程）。
        如果电梯已经过了刹得住的位置，则拒绝。

        Returns:
            'accepted'  — 已接受：清空 pending_calls，改 target_floor 为 floor
            'rejected'  — 拒绝：无法在当前位置刹停到目标楼层
            'not_running' — 电梯当前未在移动
        """
        cid = car_id
        car = self.cars[cid]
        exe = self.executors[cid]
        building = self.config['building']

        # 0. 楼层范围检查
        if not (building['min_floor'] <= floor <= building['max_floor']):
            return 'rejected'

        # 1. 必须正在运行 MOVE
        action = exe.current_action
        if action is None or action.kind not in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            return 'not_running'

        pos = car.position
        target = car.target_floor
        if pos is None or target is None:
            return 'not_running'

        # 2. 方向判断 + 缩短检查 + 刹车距离检查
        if action.kind == ActionKind.MOVE_UP:
            # 上行：change 必须在当前位置至少 1 层之上（留刹车指令下发时间）、原目标之下
            if not (pos + 1 < floor < target):
                return 'rejected'
        else:  # MOVE_DOWN
            # 下行：change 必须在当前位置至少 1 层之下、原目标之上
            if not (pos - 1 > floor > target):
                return 'rejected'

        # 3. 接受：改目标 + 清队列
        car.target_floor = floor
        self.pending_calls[cid].clear()
        return 'accepted'

    async def fireman(self, floor: int, car_id: int) -> dict:
        """救火命令：找到最近可平层停靠的楼层，先停车再换向

        核心原则：不可中途倒车——必须先在一个合法楼层完成平层停靠后，
        再改变运行方向。直接倒车会破坏楼层计数器。

        Returns:
            {'status': 'called'|'noop'|'changed'|'waypoint'|'queued',
             'waypoint': int|None}
        """
        cid = car_id
        car = self.cars[cid]
        exe = self.executors[cid]
        building = self.config['building']

        # 0. 楼层范围检查
        if not (building['min_floor'] <= floor <= building['max_floor']):
            return {'status': 'invalid'}
        if car.position is None or car.state != CarState.READY:
            return {'status': 'invalid'}

        action = exe.current_action
        pos = car.position
        target = car.target_floor

        # 1. 不在 MOVE → call
        if action is None or action.kind not in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            await self.call_internal(floor, car_id=cid)
            return {'status': 'called'}

        # 2. 已在去目标层的路上
        if floor == target:
            return {'status': 'noop'}

        if action.kind == ActionKind.MOVE_UP:
            # === 上行 ===
            # a. 顺向且刹得住：直接 change
            if pos + 1 < floor <= target:
                await self.change_internal(floor, car_id=cid)
                return {'status': 'changed'}

            # b. 刹不住 / 逆向 / 延长 → 找 waypoint
            waypoint = pos + 2
            if waypoint < target:
                # 有中间站可停靠 → 先平层到 waypoint，再倒车
                await self.change_internal(waypoint, car_id=cid)
                self.pending_calls[cid].append(floor)
                return {'status': 'waypoint', 'waypoint': waypoint}

            # 无中间站（如 pos=9→target=10）→ 等当前 MOVE 完再 call
            self.pending_calls[cid].clear()
            self.pending_calls[cid].append(floor)
            return {'status': 'queued'}

        else:
            # === 下行 ===
            # a. 顺向且刹得住：直接 change
            if pos - 1 > floor >= target:
                await self.change_internal(floor, car_id=cid)
                return {'status': 'changed'}

            # b. 刹不住 / 逆向 / 延长 → 找 waypoint
            waypoint = pos - 2
            if waypoint > target:
                # 有中间站可停靠 → 先平层到 waypoint，再倒车
                await self.change_internal(waypoint, car_id=cid)
                self.pending_calls[cid].append(floor)
                return {'status': 'waypoint', 'waypoint': waypoint}

            # 无中间站 → 等当前 MOVE 完再 call
            self.pending_calls[cid].clear()
            self.pending_calls[cid].append(floor)
            return {'status': 'queued'}

    async def reset(self, direction: str | None = None,
                    target_floor: int | None = None,
                    car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        from .player import FaultFlags
        self.cars[cid].state = CarState.UNKNOWN
        self.cars[cid].position = None
        self.cars[cid].target_floor = None
        self.cars[cid].fault = FaultFlags()
        self.pending_calls[cid].clear()
        # 同步清 executor 的 init 残留（双重保险：_start_action 也清了）
        self.executors[cid]._init_reverse_mode = False
        self.executors[cid]._init_perfect_leveling_active = False
        self.executors[cid]._init_base_segment_done = False
        # 清 executor 瞬态状态(复用 _emergency_stop 的清理模式,executor.py:401-411)
        # 不清 paused / _station_seek_enabled / manual_mode —— 这些是用户状态
        exe = self.executors[cid]
        exe.current_action = None
        exe.waiting_sensor = None
        exe.decel_state = ''
        exe._last_level_up = 0
        exe._last_level_down = 0
        exe._level_seek_active = False
        exe._level_seek_skip_next = False
        exe._level_correct_in_progress = False
        if exe._relevel_future is not None and not exe._relevel_future.done():
            exe._relevel_future.cancel()
        exe._relevel_future = None
        exe._auto_seek_active = False
        await self.cron.cancel(f'car{cid}_close_door')
        await self.cron.cancel(f'car{cid}_lights_off')
        self.pending_call_origin[cid].clear()
        self.cars[cid].human_presence = -1
        # 重置 UI 状态(逻辑状态清零 + 同步到 IO)
        self.cars[cid].ui = IndicatorState()
        await self.ui[cid].sync_to_io()
        # 清空动作队列:避免旧 MOVE 在新 INITIALIZE _start_action 覆盖前污染状态
        while not self.action_queues[cid].empty():
            try:
                self.action_queues[cid].get_nowait()
            except asyncio.QueueEmpty:
                break
        if direction:
            self.executors[cid].init_direction = direction
        tf = target_floor if target_floor is not None else 1
        action = Action(ActionKind.INITIALIZE, floor=tf)
        await self.action_queues[cid].put(action)

    async def reload(self) -> None:
        self._load_config()
        self.mapper.reload()
        self.display.reload()
        building = self.config['building']
        io2http = self.config['io2http']
        self.io._tick_interval = max(0.01, io2http.get('tick_interval_ms', 100) / 1000.0)
        for cid in self.car_ids:
            self.executors[cid].top_base_floor = building['top_base_floor']
            self.executors[cid].bottom_base_floor = building['bottom_base_floor']
            self.executors[cid].init_direction = self.config['elevator']['initialization_direction']
        # 站点吸附开关 reload 同步
        station_seek_enabled = self.config['elevator'].get('station_seek', False)
        for cid in self.car_ids:
            self.executors[cid].set_station_seek(station_seek_enabled)
        print(f'[reload] config reloaded: init_dir={self.config["elevator"]["initialization_direction"]}, '
              f'station_seek={station_seek_enabled}')

    async def set_station_seek(self, enabled: bool) -> dict[str, int]:
        """切换站点吸附开关（高层 API，console 用）

        对所有轿厢:打开 flag。
        开启时,空闲且传感器在 (0,0) 的车入队 INITIALIZE down 1(自动寻站);
        空闲且在平层位的车立即激活 hold。
        忙车只 set flag,等下次 _arrive_and_brake 兜底。

        返回:{auto_seek_count, activate_count, skipped_count} 给 console 展示。
        """
        auto_seek_count = 0
        activate_count = 0
        skipped_count = 0

        for cid in self.car_ids:
            exe = self.executors[cid]
            # 先设置 flag（executor 内部清/保留激活态）
            exe.set_station_seek(enabled)

            if not enabled:
                continue

            # 跳过手动模式(executor.paused=True)的车
            if exe.paused:
                skipped_count += 1
                continue

            # 正在跑的车:只 set flag,等 _arrive_and_brake
            if exe.current_action is not None:
                skipped_count += 1
                continue

            # 空闲车:读 level 传感器判断是否需要 auto-seek
            try:
                up_addr = self.mapper.db_to_i(
                    self.mapper.addr_input('level_up', cid)
                )
                dn_addr = self.mapper.db_to_i(
                    self.mapper.addr_input('level_down', cid)
                )
            except KeyError:
                # 没有 level 信号（异常配置）→ 仅激活 hold,让下次 IO 事件触发检查
                exe._level_seek_active = True
                asyncio.create_task(exe._level_seek_check())
                activate_count += 1
                continue

            up_now = self.io.get_input(up_addr)
            dn_now = self.io.get_input(dn_addr)

            if up_now == 0 and dn_now == 0:
                # 车散在楼层之间 → auto-seek: 直接下跑找最近一个 (↑1↓1)
                # 不入队 INITIALIZE（不需要反向、不需要计数到 L1）
                await exe.start_auto_seek_down()
                auto_seek_count += 1
            else:
                # 已在平层区(含偏离 1,0 / 0,1),立即激活 hold
                exe._level_seek_active = True
                asyncio.create_task(exe._level_seek_check())
                activate_count += 1

        return {
            'auto_seek_count': auto_seek_count,
            'activate_count': activate_count,
            'skipped_count': skipped_count,
        }

    def station_seek_enabled(self) -> bool:
        """是否有任意一部车的吸附开启"""
        return any(
            self.executors[cid].is_station_seek_enabled()
            for cid in self.car_ids
        )

    # ===== 用户模式（usermode） =====

    @property
    def usermode_enabled(self) -> bool:
        return self._usermode

    async def set_usermode(self, enabled: bool) -> dict:
        """切换用户模式

        启用时：验证所有轿厢已初始化（state=READY + position 非空）
               → 设置 ready 信号为 1，PLC 认为电梯准备就绪
        禁用时：设置 ready 信号为 0

        Returns:
            {'enabled': bool, 'blocked': list[int]}
            blocked 列出未就绪的轿厢 ID（空列表 = 全部就绪）
        """
        result: dict[str, object] = {'enabled': enabled, 'blocked': []}

        if enabled:
            blocked: list[int] = []
            for cid in self.car_ids:
                car = self.cars[cid]
                if car.state != CarState.READY or car.position is None:
                    blocked.append(cid)
            if blocked:
                result['blocked'] = blocked
                return result  # 拒绝启用，不设 ready

            self._usermode = True
            try:
                ready_addr = self.mapper.addr_output('ready', 0)
                await self.io.set(ready_addr, 1)
            except KeyError:
                pass
        else:
            self._usermode = False
            try:
                ready_addr = self.mapper.addr_output('ready', 0)
                await self.io.set(ready_addr, 0)
            except KeyError:
                pass

        return result

    async def manual_batch(self, direction: Direction | None,
                           high_speed: bool, car_ids: list[int]) -> None:
        """批量手动方向"""
        for cid in car_ids:
            self.manual_mode[cid] = True
            car = self.cars[cid]
            if direction == Direction.UP:
                if car.direction == Direction.UP and car.manual_speed == high_speed:
                    continue
                car.direction = Direction.UP
                car.manual_speed = high_speed
                await self.executors[cid].motor.start(high_speed=high_speed, direction='up')
            elif direction == Direction.DOWN:
                if car.direction == Direction.DOWN and car.manual_speed == high_speed:
                    continue
                car.direction = Direction.DOWN
                car.manual_speed = high_speed
                await self.executors[cid].motor.start(high_speed=high_speed, direction='down')
            else:  # stop
                if car.direction == Direction.IDLE and car.manual_speed is False:
                    continue
                await self.executors[cid].motor.stop()
                car.direction = Direction.IDLE
                car.manual_speed = False

    async def manual_brake_batch(self, level: int,
                                 car_ids: list[int]) -> None:
        """批量刹车"""
        for cid in car_ids:
            await self.executors[cid].motor.set_brake_level(level)

    async def manual_up(self, high_speed: bool = True,
                        car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = True
        car = self.cars[cid]
        if car.direction == Direction.UP and car.manual_speed == high_speed:
            return
        car.direction = Direction.UP
        car.manual_speed = high_speed
        await self.executors[cid].motor.start(high_speed=high_speed, direction='up')

    async def manual_down(self, high_speed: bool = True,
                          car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = True
        car = self.cars[cid]
        if car.direction == Direction.DOWN and car.manual_speed == high_speed:
            return
        car.direction = Direction.DOWN
        car.manual_speed = high_speed
        await self.executors[cid].motor.start(high_speed=high_speed, direction='down')

    async def manual_stop(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        car = self.cars[cid]
        if car.direction == Direction.IDLE and car.manual_speed is False:
            return
        await self.executors[cid].motor.stop()
        car.direction = Direction.IDLE
        car.manual_speed = False

    async def manual_brake(self, level: int | None = None,
                           car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        exe = self.executors[cid]
        if level is None:
            level = exe.manual_brake_level
        else:
            exe.manual_brake_level = level
        if exe.manual_current_brake_state == level:
            return
        exe.manual_current_brake_state = level
        await exe.motor.set_brake_level(level)

    async def manual_emergency_stop(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = False
        await self.executors[cid]._emergency_stop(reason='manual_e_stop')

    async def manual_auto(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        await self.manual_brake(0, car_id=cid)
        await self.manual_stop(car_id=cid)
        self.manual_mode[cid] = False

    async def clear_outputs(self) -> None:
        for cid in self.car_ids:
            writes: dict[str, int] = {}
            for sig in self.mapper.all_output_signals(cid):
                if sig == 'ready':
                    continue
                try:
                    db_addr = self.mapper.addr_output(sig, cid)
                    writes[db_addr] = 0
                except KeyError:
                    continue
            await self.io_write[cid].set_many(writes)
        # 全局共享输出(ready 等)用 self.io 清
        global_writes: dict[str, int] = {}
        for sig in self.mapper.all_output_signals(0):
            try:
                db_addr = self.mapper.addr_output(sig, 0)
                global_writes[db_addr] = 0
            except KeyError:
                continue
        if global_writes:
            await self.io.set_many(global_writes)
        # 清空逻辑状态(UI + hall indicator)
        for cid in self.car_ids:
            self.cars[cid].ui = IndicatorState()
        self._hall_indicator_state.clear()
        print(f'[clear] 所有输出已置零')

    async def set_hall_indicator(self, floor: int, direction: str,
                                  on: bool) -> None:
        """外召按钮指示灯(建筑级信号,不属于任何轿厢)

        Args:
            floor: 楼层号 (up: 1..9, down: 2..10)
            direction: 'up' | 'down'
            on: True=亮, False=灭
        """
        if direction not in ('up', 'down'):
            raise ValueError(
                f"direction 必须是 'up' 或 'down',got {direction!r}"
            )
        self._hall_indicator_state[(floor, direction)] = on
        sig = f'hall_indicator_{direction}_{floor}'
        await self.io.set(self.mapper.addr_output(sig, 0), 1 if on else 0)

    def hall_indicator_state(self, floor: int, direction: str) -> bool:
        """读当前外召按钮指示灯状态(供 /buttonui toggle 用)"""
        return self._hall_indicator_state.get((floor, direction), False)

    # ===== /door 命令 =====

    def _door_precheck(self, car_id: int, action: str,
                        force: bool) -> dict | None:
        """预检:不通过返回 dict 错误,通过返回 None

        检查项:
            - car_door_lock 信号存在
            - init 检查 (force 跳过)
            - 移动检查 (始终生效,force 也不越过)
            - 门已开/已关 检查 (force 跳过)
        """
        car = self.cars[car_id]
        io = self.io
        mapper = self.mapper

        try:
            car_lock_i = mapper.db_to_i(
                mapper.addr_input('car_door_lock', car_id)
            )
        except KeyError:
            return {
                'status': 'rejected',
                'message': f'car {car_id} io_config 缺 car_door_lock 信号',
            }
        car_door_locked = bool(io.get_input(car_lock_i))

        if not force and car.state != CarState.READY:
            return {
                'status': 'rejected',
                'message': f'car {car_id} 未初始化,需要 force 参数强制执行',
            }
        if car.direction != Direction.IDLE:
            return {
                'status': 'rejected',
                'message': f'car {car_id} 正在移动({car.direction.value}),无法执行门操作',
            }
        if not force:
            if action == 'open' and not car_door_locked:
                return {
                    'status': 'rejected',
                    'message': f'car {car_id} 门已开(car_door_lock=false),无需再开',
                }
            if action == 'close' and car_door_locked:
                return {
                    'status': 'rejected',
                    'message': f'car {car_id} 门已关好(car_door_lock=true),无需再关',
                }
        return None  # 通过

    async def control_door(self, car_id: int, action: str,
                           force: bool = False) -> dict:
        """控制开门/关门。**非阻塞**,预检后立即返回 dispatched,后台跟踪完成。

        Args:
            car_id: 轿厢 ID
            action: 'open' | 'close'
            force: 跳过部分预检,直接拉 relay 立即返回

        Returns:
            dict {
                'status': 'dispatched' | 'rejected' | 'busy' | 'force_done',
                'message': str,
            }

            - 'dispatched': 已派发 action + 后台 task 跟踪,REPL 不阻塞
                           完成 / 错层 由后台 task / /debug show door_status 输出
            - 'force_done': force 模式已拉 relay
            - 'rejected' / 'busy': 命令级错误,命令立即打印
        """
        door = self.executors[car_id].door
        mapper = self.mapper
        io = self.io

        # 1. 预检 (同步,极快)
        precheck_result = self._door_precheck(car_id, action, force)
        if precheck_result is not None:
            return precheck_result

        # 2. force: 直接拉 relay + 立即返回
        if force:
            if action == 'open':
                await door.open()
            else:
                await door.close()
            return {
                'status': 'force_done',
                'message': f'car {car_id} force 模式:已拉 {action} relay(不等待门锁)',
            }

        # 3. 同轿厢互斥
        if self._door_busy[car_id]:
            return {
                'status': 'busy',
                'message': f'car {car_id} 门动作进行中,请等待完成',
            }
        self._door_busy[car_id] = True

        # 4. 注册 listener (同步,在 dispatch 前,防止 race)
        floor_lock_i: dict[int, str] = {}
        for f in range(1, 11):
            try:
                floor_lock_i[f] = mapper.db_to_i(
                    mapper.addr_input(f'floor_door_lock_{f}', car_id)
                )
            except KeyError:
                pass

        door_done_signal = 'door_open_done' if action == 'open' else 'door_close_done'
        try:
            door_done_i = mapper.db_to_i(
                mapper.addr_input(door_done_signal, car_id)
            )
        except KeyError:
            self._door_busy[car_id] = False
            return {
                'status': 'rejected',
                'message': f'car {car_id} io_config 缺 {door_done_signal} 信号',
            }

        done_event = asyncio.Event()
        wrong_floor: list[int] = []
        car = self.cars[car_id]
        pos_at_dispatch = car.position

        async def listener(event: IOEvent) -> None:
            if event.i_addr == door_done_i and event.bit == 1:
                done_event.set()
            elif event.i_addr in floor_lock_i.values():
                if event.bit == 0:
                    f = next(
                        fl for fl, addr in floor_lock_i.items()
                        if addr == event.i_addr
                    )
                    if f != pos_at_dispatch:
                        wrong_floor.append(f)

        io.add_listener(listener)

        # 5. 派发 action
        try:
            kind = ActionKind.OPEN_DOOR if action == 'open' else ActionKind.CLOSE_DOOR
            await self.action_queues[car_id].put(Action(kind))
        except Exception:
            # 入队失败也清理
            io.remove_listener(listener)
            self._door_busy[car_id] = False
            raise

        # 6. 后台 task 跟踪完成 + 错层检测
        # 注意:create_task 不阻塞当前 coroutine
        asyncio.create_task(self._door_track_completion(
            car_id=car_id,
            action=action,
            listener=listener,
            done_event=done_event,
            wrong_floor=wrong_floor,
        ))

        # 7. cron 兜底:PLC 异常不发 done 信号时,timeout 秒后强制释放 mutex
        # 无 sleep / wait_for,完全由 cron 事件循环驱动
        timeout = self.config.get('elevator', {}).get(
            'door_complete_timeout', 8)
        job_name = f'door_timeout_{car_id}_{action}'

        async def _timeout_cb():
            await self._door_timeout_callback(
                car_id, action, listener, done_event)

        try:
            await self.cron.schedule(CronJob(
                name=job_name,
                trigger_time=time.monotonic() + timeout,
                delay=timeout,
                action=_timeout_cb,
            ))
        except Exception:
            # 兜底调度失败不应影响主流程(派发已成功,后台 task 仍在跟踪)
            pass

        # 8. 立即返回,不阻塞 REPL
        return {
            'status': 'dispatched',
            'message': f'car {car_id} 已派发 {action} 命令,后台跟踪中',
        }

    async def _door_track_completion(
        self,
        car_id: int,
        action: str,
        listener,
        done_event: asyncio.Event,
        wrong_floor: list[int],
    ) -> None:
        """后台跟踪任务:等 door_open_done/close_done + 错层检测 + 释放 mutex

        行为:
            - 错层(仅 open):始终打印 ⚠️ (不需 debug 开关)
            - 成功完成:不打印 (由 /debug show door_status 控制)
            - 释放 mutex + 注销 listener(无论结果)
            - 取消 cron 兜底 job(避免 timeout 误触发)
        """
        try:
            await done_event.wait()
            # 成功路径:取消 cron 兜底(若还没 fire)
            await self.cron.cancel(f'door_timeout_{car_id}_{action}')
            if wrong_floor and action == 'open':
                wf = wrong_floor[0]
                pos = self.cars[car_id].position
                print(
                    f'[car {car_id}] ⚠️  开错楼:car 在 L{pos},'
                    f'但 L{wf} 层门锁打开了'
                )
                print(
                    f'         → 需手动 /door {car_id} close force '
                    f'或 /car {car_id} init down 1 重置'
                )
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self.io.remove_listener(listener)
            except Exception:
                pass
            self._door_busy[car_id] = False

    async def _door_timeout_callback(
        self,
        car_id: int,
        action: str,
        listener,
        done_event: asyncio.Event,
    ) -> None:
        """门动作完成超时兜底(cron 触发,无 sleep / wait)

        PLC 异常不发 door_open_done / door_close_done 时,
        强制释放 _door_busy 防锁死,set done_event 唤醒后台 task。
        """
        if not self._door_busy.get(car_id, False):
            return  # 正常完成路径已释放,no-op
        print(
            f'[car {car_id}] ⚠️  门动作超时(无 door_{action}_done 信号),'
            f'强制释放 mutex'
        )
        print(
            f'         → 需手动检查 PLC 状态,后续 /door 命令可正常执行'
        )
        done_event.set()  # 唤醒后台 task
        try:
            self.io.remove_listener(listener)
        except Exception:
            pass
        self._door_busy[car_id] = False

    def status_snapshot(self, car_id: int | None = None) -> dict[str, Any]:
        cid = car_id if car_id is not None else self.current_car_id
        return {
            'car': self.cars[cid].snapshot(),
            'algorithm': self.algorithm.name,
            'pending_calls': list(self.pending_calls[cid]),
            'action_queue_size': self.action_queues[cid].qsize(),
            'init_direction': self.executors[cid].init_direction,
            'simulate': self.simulate,
            'manual_mode': self.manual_mode[cid],
            'usermode': self._usermode,
        }
