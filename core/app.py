"""
app.py —— 异步装配与主循环

职责:
    - 加载所有 config
    - 装配 IOClient / IOMapper / DisplayEncoder / Car / Executor / Algorithm
    - 启动后台任务（executor 循环 + IO 事件监听）
    - 暴露高层 API 给 console 调用（call / reset / status / etc.）
"""

import asyncio
from pathlib import Path
from typing import Any

import yaml

from .actions import Action, ActionKind, ActionQueue
from .algorithm import ALGORITHM_REGISTRY, ElevatorAlgorithm, get_algorithm
from .display import DisplayEncoder
from .executor import ActionExecutor
from .io_client import IOClient, IOEvent
from .io_mapper import IOMapper
from .player import Car, CarState, Direction


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

        # ===== 装配 =====
        io2http = self.config['io2http']
        self.io = IOClient(
            http_url=io2http['http_url'],
            ws_url=io2http['ws_url'],
            simulate=simulate,
            debug=False,
        )
        self.mapper = IOMapper(io_config_path)
        self.display = DisplayEncoder(display_config_path)
        self.car = Car(car_id=int(self.config['elevator']['car_id']))
        self.action_queue = ActionQueue()

        algo_name = self.config['algorithm']['name']
        self.algorithm: ElevatorAlgorithm = get_algorithm(algo_name)

        self.executor = ActionExecutor(
            car=self.car,
            io=self.io,
            mapper=self.mapper,
            display=self.display,
            car_id=int(self.config['elevator']['car_id']),
            init_direction=self.config['elevator']['initialization_direction'],
            top_base_floor=self.config['building']['top_base_floor'],
            bottom_base_floor=self.config['building']['bottom_base_floor'],
            on_action_done=self._on_action_done,
            on_emergency_stop=self._on_emergency_stop,
        )

        # 内部状态
        self.pending_calls: list[int] = []
        self.manual_mode: bool = False       # True=手动控制，False=算法自动
        self._executor_task: asyncio.Task | None = None
        self.debug = False

    def _load_config(self) -> None:
        with self.config_path.open('r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

    # ===== 生命周期 =====

    async def start(self) -> None:
        await self.io.start()
        self.io.add_listener(self._on_io_event)
        # 后台跑 executor 主循环
        self._executor_task = asyncio.create_task(
            self.executor.run_loop(self.action_queue)
        )
        # 不自动 INITIALIZE——让用户手动发 /car N init <dir> <floor>
        # 避免电梯在端站时自动启动撞上 2 限位（坠机限位）

    async def stop(self) -> None:
        if self._executor_task and not self._executor_task.done():
            self._executor_task.cancel()
            try:
                await self._executor_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.io.stop()

    # ===== 协调 =====

    async def _tick(self) -> None:
        """让算法根据当前状态决定下一步动作，推入队列"""
        if self.debug:
            print(f'[tick] car={self.car} pending={self.pending_calls}')
        actions = self.algorithm.decide(self.car, self.pending_calls)
        for action in actions:
            if self.debug:
                print(f'[tick]   → {action}')
            await self.action_queue.put(action)

    async def _on_action_done(self, last_action: Action) -> None:
        """执行器完成一个动作后回调，重新 tick 并在合适时清理 pending_calls"""
        # 关门完成 = 任务真正完成，从 pending_calls 中移除目标
        if last_action is not None and last_action.kind == ActionKind.CLOSE_DOOR:
            if self.car.target_floor is not None and self.car.position == self.car.target_floor:
                self.pending_calls = [
                    c for c in self.pending_calls if c != self.car.target_floor
                ]
                self.car.target_floor = None

        # INITIALIZE 完成后，如果目标楼层 != 基站楼层，自动移动到目标层
        if last_action is not None and last_action.kind == ActionKind.INITIALIZE:
            target = last_action.floor
            if target is not None and target != self.car.position:
                self.car.target_floor = target
                dir_action = Action(
                    ActionKind.MOVE_UP if target > self.car.position else ActionKind.MOVE_DOWN
                )
                await self.action_queue.put(dir_action)
                return  # 不调 tick（MOVE 完成后会再回调）
        await self._tick()

    async def _on_io_event(self, event: IOEvent) -> None:
        """IO 变化事件 → executor 推进 + 故障标志更新"""
        await self.executor.on_io_event(event)
        # IO 事件也可能需要重新 tick（比如门开了、关到位了）
        # 但动作 done 已经会 tick，简化：IO 事件不主动 tick

    async def _on_emergency_stop(self) -> None:
        """紧急停止回调：清 pending，清 manual_mode，让算法进入故障冻结"""
        self.pending_calls.clear()
        self.car.target_floor = None
        self.manual_mode = False
        print('[emergency] 紧急停止：清空 pending，切回 auto（不可调度）')

    # ===== 高层 API（给 console 用） =====

    async def call_internal(self, floor: int) -> None:
        """内召：到目标楼层"""
        if floor in self.pending_calls:
            return
        self.pending_calls.append(floor)
        self.car.target_floor = floor
        await self._tick()

    async def reset(self, direction: str | None = None,
                    target_floor: int | None = None) -> None:
        """手动触发初始化（/car N init <dir> <floor>）

        直接推 INITIALIZE action 到队列（跳过算法层），
        方向 = direction 或 config 默认，目标楼层 = target_floor 或 1
        """
        from .player import FaultFlags
        self.car.state = CarState.UNKNOWN
        self.car.position = None
        self.car.target_floor = None
        self.car.fault = FaultFlags()
        self.pending_calls.clear()
        if direction:
            self.executor.init_direction = direction
        tf = target_floor if target_floor is not None else 1
        action = Action(ActionKind.INITIALIZE, floor=tf)
        await self.action_queue.put(action)

    async def set_algorithm(self, name: str) -> None:
        """热切换算法"""
        self.algorithm = get_algorithm(name)
        self.config['algorithm']['name'] = name
        await self._tick()

    async def reload(self) -> None:
        """重新读 config + io_config + display_config"""
        self._load_config()
        self.mapper.reload()
        self.display.reload()
        self.executor.init_direction = self.config['elevator']['initialization_direction']
        print(f'[reload] config reloaded: '
              f'algorithm={self.config["algorithm"]["name"]} '
              f'init_dir={self.executor.init_direction}')

    async def set_display(self, glyph_or_floor) -> None:
        """手动设置显示（调试用）"""
        # 接受楼层号或字符
        try:
            floor = int(glyph_or_floor)
            action = Action(ActionKind.SET_DISPLAY, floor=floor)
        except (ValueError, TypeError):
            action = Action(ActionKind.SET_DISPLAY, floor=None)
        await self.action_queue.put(action)

    async def manual_up(self, high_speed: bool = True) -> None:
        """手动上行（幂等：重复调用安全，已在向上+同速度时跳过 IO 写）"""
        self.manual_mode = True
        if self.car.direction == Direction.UP and self.car.manual_speed == high_speed:
            return  # 已在向上同速，不重复写
        self.car.direction = Direction.UP
        self.car.manual_speed = high_speed
        car_id = self.car.car_id
        await self.io.set_many({
            self.mapper.addr_output('up_contactor', car_id): 1,
            self.mapper.addr_output('down_contactor', car_id): 0,
            self.mapper.addr_output('high_speed_contactor', car_id): 1 if high_speed else 0,
            self.mapper.addr_output('low_speed_contactor', car_id): 0 if high_speed else 1,
            self.mapper.addr_output('motor_start', car_id): 1,
        })

    async def manual_down(self, high_speed: bool = True) -> None:
        """手动下行（幂等）"""
        self.manual_mode = True
        if self.car.direction == Direction.DOWN and self.car.manual_speed == high_speed:
            return
        self.car.direction = Direction.DOWN
        self.car.manual_speed = high_speed
        car_id = self.car.car_id
        await self.io.set_many({
            self.mapper.addr_output('up_contactor', car_id): 0,
            self.mapper.addr_output('down_contactor', car_id): 1,
            self.mapper.addr_output('high_speed_contactor', car_id): 1 if high_speed else 0,
            self.mapper.addr_output('low_speed_contactor', car_id): 0 if high_speed else 1,
            self.mapper.addr_output('motor_start', car_id): 1,
        })

    async def manual_stop(self) -> None:
        """停电机（幂等，松开方向键时调用）"""
        if self.car.direction == Direction.IDLE and self.car.manual_speed is False:
            return
        car_id = self.car.car_id
        await self.io.set_many({
            self.mapper.addr_output('up_contactor', car_id): 0,
            self.mapper.addr_output('down_contactor', car_id): 0,
            self.mapper.addr_output('high_speed_contactor', car_id): 0,
            self.mapper.addr_output('low_speed_contactor', car_id): 0,
            self.mapper.addr_output('motor_start', car_id): 0,
        })
        self.car.direction = Direction.IDLE
        self.car.manual_speed = False

    async def manual_brake(self, level: int | None = None) -> None:
        """手动刹车（幂等：相同档位不重复写）

        8 档含义: 0=释放, 1=1级, 2=2级, 3=1+2, 4=3级, 5=1+3, 6=2+3, 7=全刹
        """
        if level is None:
            level = self.executor.manual_brake_level
        else:
            self.executor.manual_brake_level = level
        if self.executor.manual_current_brake_state == level:
            return  # 已经是这档
        self.executor.manual_current_brake_state = level
        b1 = 1 if (level & 0b001) else 0
        b2 = 1 if (level & 0b010) else 0
        b3 = 1 if (level & 0b100) else 0
        car_id = self.car.car_id
        await self.io.set_many({
            self.mapper.addr_output('brake_1', car_id): b1,
            self.mapper.addr_output('brake_2', car_id): b2,
            self.mapper.addr_output('brake_3', car_id): b3,
        })

    async def manual_emergency_stop(self) -> None:
        """紧急停止 —— 清所有输出 + 状态置 FAULT"""
        self.manual_mode = False
        await self.executor._emergency_stop(reason='manual_e_stop')

    async def manual_auto(self) -> None:
        """切回自动控制：释放刹车、停电机、清 manual_mode、算法接管"""
        await self.manual_brake(0)
        await self.manual_stop()
        self.manual_mode = False

    async def clear_outputs(self) -> None:
        """将所有输出位置零（清 DB11 所有信号，不含 ready 信号）"""
        car_id = int(self.config['elevator']['car_id'])
        writes: dict[str, int] = {}
        for sig in self.mapper.all_output_signals(car_id):
            if sig == 'ready':
                continue  # 保留准备就绪信号
            try:
                db_addr = self.mapper.addr_output(sig, car_id)
                writes[db_addr] = 0
            except KeyError:
                continue
        # 也清全局输出（hall_indicator）
        for sig in self.mapper.all_output_signals(0):
            try:
                db_addr = self.mapper.addr_output(sig, 0)
                writes[db_addr] = 0
            except KeyError:
                continue
        await self.io.set_many(writes)
        print(f'[clear] 所有输出已置零')

    def status_snapshot(self) -> dict[str, Any]:
        return {
            'car': self.car.snapshot(),
            'algorithm': self.algorithm.name,
            'pending_calls': list(self.pending_calls),
            'action_queue_size': self.action_queue.qsize(),
            'init_direction': self.executor.init_direction,
            'simulate': self.simulate,
            'manual_mode': self.manual_mode,
        }

    def available_algorithms(self) -> list[str]:
        return list(ALGORITHM_REGISTRY)