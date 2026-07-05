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
from pathlib import Path
from typing import Any

import yaml

from .actions import Action, ActionKind, ActionQueue
from .algorithm import ElevatorAlgorithm, get_algorithm
from .display import DisplayEncoder
from .executor import ActionExecutor
from .io_client import IOClient, IOEvent
from .io_mapper import IOMapper
from .player import Car, CarState, Direction
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
            )

        self._executor_task: asyncio.Task | None = None
        self.debug = False

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

    # ===== 协调（按轿厢） =====

    async def _tick(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        if self.debug:
            print(f'[tick] car={cid} pos={self.cars[cid].position} '
                  f'pending={self.pending_calls[cid]}')
        actions = self.algorithm.decide(self.cars[cid], self.pending_calls[cid])
        for action in actions:
            if self.debug:
                print(f'[tick]   → {action}')
            await self.action_queues[cid].put(action)

    def _make_on_action_done(self, car_id: int):
        async def _on_action_done(last_action: Action) -> None:
            await self._on_action_done(car_id, last_action)
        return _on_action_done

    async def _on_action_done(self, car_id: int, last_action: Action) -> None:
        if last_action is not None and last_action.kind in (
            ActionKind.MOVE_UP, ActionKind.MOVE_DOWN
        ):
            pos = self.cars[car_id].position
            target = self.cars[car_id].target_floor
            if target is not None and pos == target:
                self.pending_calls[car_id] = [
                    c for c in self.pending_calls[car_id] if c != target
                ]
                self.cars[car_id].target_floor = None

        if last_action is not None and last_action.kind == ActionKind.INITIALIZE:
            target = last_action.floor
            if target is not None and target != self.cars[car_id].position:
                self.cars[car_id].target_floor = target
                dir_action = Action(
                    ActionKind.MOVE_UP if target > self.cars[car_id].position
                    else ActionKind.MOVE_DOWN
                )
                await self.action_queues[car_id].put(dir_action)
                return
        await self._tick(car_id)

    def _make_on_emergency_stop(self, car_id: int):
        async def on_emergency():
            self.pending_calls[car_id].clear()
            self.cars[car_id].target_floor = None
            self.manual_mode[car_id] = False
            print(f'[emergency] car {car_id} 紧急停止')
        return on_emergency

    # ===== 高层 API（给 console 用） =====

    async def call_internal(self, floor: int, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        if floor in self.pending_calls[cid]:
            return
        self.pending_calls[cid].append(floor)
        self.cars[cid].target_floor = floor
        await self._tick(cid)

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
        self.executors[cid]._init_last_reverse_pos = None
        self.executors[cid]._init_reverse_start_time = None
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
        print(f'[reload] config reloaded: init_dir={self.config["elevator"]["initialization_direction"]}')

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
        print(f'[clear] 所有输出已置零')

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
        }
