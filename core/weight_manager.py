"""
weight_manager.py —— 电梯重量三态机管理器（小脑模块）

职责:
    - 维护 Car.weight_state（0=正常 / 1=临界 / 2=超重）
    - 维护 Car.weight_kg（最后一次 read_word 的值）
    - 状态 2 时：复用 cancel_for_reopen 开门 + 亮满载灯 + 启动应急 polling
    - 应急 polling：每 weight_poll_interval_ms 查 word，weight ≤ max 退出

大脑（passenger/algorithm）只读 car.weight_state 做决策，不碰 read_word。
"""

import asyncio
from typing import TYPE_CHECKING, Callable, Awaitable

if TYPE_CHECKING:
    from .app import App
    from .player import Car
from .player import DoorState


class WeightManager:
    """重量三态机管理器（每 app 一个实例）"""

    def __init__(self, app: 'App',
                 on_weight_event: Callable[[int, int, int, int], Awaitable[None]] | None = None
                 ) -> None:
        self._app = app
        self._poll_tasks: dict[int, asyncio.Task] = {}
        self._poll_interval_ms: int = app.config.get('weight_poll_interval_ms', 200)
        self.on_weight_event = on_weight_event

    def _log(self, msg: str) -> None:
        """输出到 stderr（已被 App 替换为 TeeStderr → 自动写文件+终端）"""
        import sys
        sys.stderr.write(msg + '\n')
        sys.stderr.flush()

    # ===== 三个事件钩子 =====

    async def on_close_door_starting(self, car_id: int) -> bool:
        """H1: 关门动作开始时触发（exector 收到 CLOSE_DOOR 前）

        查 weight → 更新 state。state=2 时拦截关门，走 reopen 流程 + 启 polling。
        返回 True=跳过关门（state=2 或 read 失败），False=正常关门。
        """
        car = self._app.cars[car_id]
        weight = await self._read_weight(car_id)
        if weight is None:
            return False  # read 失败，放行（fail-open）
        self._update_weight_state(car, weight)
        if car.weight_state == 2:
            await self._handle_overweight(car_id)
            return True  # 拦截关门
        return False

    async def on_close_door_completed(self, car_id: int) -> None:
        """H2: 关门动作完成时触发（_on_door_closed 末尾）

        查 weight → 更新 state（仅 0↔1 切换）。
        """
        car = self._app.cars[car_id]
        weight = await self._read_weight(car_id)
        if weight is None:
            return
        old_state = car.weight_state
        self._update_weight_state(car, weight)
        # 状态 2 期间关门被强制打断 → 这里不会被调到；若 state 在 0/1/2 间切换，仅回写
        if car.weight_state == 2 and old_state != 2:
            await self._handle_overweight(car_id)

    async def on_door_open_button_pressed(self, car_id: int) -> None:
        """H3: 开门按钮按下时触发（仅同步状态，不改变开门动作）"""
        weight = await self._read_weight(car_id)
        if weight is None:
            return
        car = self._app.cars[car_id]
        self._update_weight_state(car, weight)

    # ===== 状态计算 =====

    def _update_weight_state(self, car: 'Car', weight: int) -> None:
        """根据 weight 和 car.max_weight / car.weight_threshold_kg 计算新 state"""
        old_state = car.weight_state
        old_weight = car.weight_kg
        car.weight_kg = weight
        MAX = car.max_weight
        THRESHOLD = car.weight_threshold_kg
        if MAX <= 0:
            car.weight_state = 0
            if self.on_weight_event is not None and (weight != old_weight or old_state != 0):
                _fire = self.on_weight_event(car.car_id, weight, old_state, 0)
                asyncio.create_task(_fire)
            return
        if weight > MAX:
            car.weight_state = 2
        elif weight >= THRESHOLD:
            car.weight_state = 1
        else:
            car.weight_state = 0
        # fire 事件（每次查询都通知）
        if self.on_weight_event is not None:
            _fire = self.on_weight_event(car.car_id, weight, old_state, car.weight_state)
            asyncio.create_task(_fire)

    # ===== 状态 2 紧急开门 =====

    async def _handle_overweight(self, car_id: int) -> None:
        """状态 2 紧急开门：复用 cancel_for_reopen 流程"""
        car = self._app.cars[car_id]
        # 只有门关着/正在关时才需要开
        if car.door_state in (DoorState.CLOSING, DoorState.CLOSED):
            # 复用开门按钮按下流程
            self._app.executors[car_id].door.cancel_for_reopen()
            from .executor import Action, ActionKind
            await self._app.action_queues[car_id].put(Action(ActionKind.OPEN_DOOR))
        # 亮满载灯
        try:
            await self._app.ui[car_id].set_full_load(True)
        except (KeyError, AttributeError):
            pass
        # 启动应急 polling
        self._start_weight_emergency_poll(car_id)
        self._log(f'[weight] car{car_id} 超重 (state=2, weight={car.weight_kg}kg), '
              f'开门 + 亮满载灯 + 启动 polling')

    # ===== 应急 polling =====

    def _start_weight_emergency_poll(self, car_id: int) -> None:
        """启动应急 polling（幂等，已在跑则不重复启动）"""
        existing = self._poll_tasks.get(car_id)
        if existing and not existing.done():
            return
        self._poll_tasks[car_id] = asyncio.create_task(
            self._weight_emergency_poll(car_id))

    async def _weight_emergency_poll(self, car_id: int) -> None:
        """仅在 state=2 期间运行，每 poll_interval_ms 查 word，weight ≤ max 退出"""
        car = self._app.cars[car_id]
        interval = self._poll_interval_ms / 1000.0
        while True:
            await asyncio.sleep(interval)
            weight = await self._read_weight(car_id)
            if weight is None:
                continue  # read 失败，继续等
            if weight <= car.max_weight:
                # 退出 polling
                self._update_weight_state(car, weight)
                # 熄满载灯
                try:
                    await self._app.ui[car_id].set_full_load(False)
                except (KeyError, AttributeError):
                    pass
                # 降级后门仍开着 → 重新关门（已有人下完/重量回落）
                if car.door_state == DoorState.OPEN:
                    from .executor import Action, ActionKind
                    await self._app.action_queues[car_id].put(
                        Action(ActionKind.CLOSE_DOOR))
                state_label = {0: '正常', 1: '临界', 2: '超重'}
                self._log(f'[weight] car{car_id} polling 退出, '
                      f'weight={car.weight_kg}kg → state={car.weight_state}({state_label.get(car.weight_state, "?")})')
                return

    # ===== 公共查询 =====

    def is_overloaded(self, car_id: int) -> bool:
        """大脑查询：返回 True 表示该车不应响应外呼（state=1 或 2）"""
        car = self._app.cars[car_id]
        return car.weight_state >= 1 if car.max_weight > 0 else False

    # ===== 内部 read_word 封装 =====

    async def _read_weight(self, car_id: int) -> int | None:
        """读 word（封装 mapper.addr_word_input + io.read_word + vplc）

        返回 None = 当前 profile 无 weight_word 配置 / read 失败
        """
        try:
            db_num, byte = self._app.mapper.addr_word_input('weight', car_id)
        except KeyError:
            return None  # 当前 profile 无 weight_word，silent skip
        if self._app.io.simulate:
            return self._app.virtual_plcs[car_id].read_word(db_num, byte)
        return await self._app.io.read_word(db_num, byte)
