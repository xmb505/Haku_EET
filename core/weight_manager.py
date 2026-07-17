"""
weight_manager.py —— 电梯重量三态机管理器（小脑模块）

职责:
    - 在 executor 重量轮询更新 car.weight_state 后，触发副作用动作
    - 状态 2 时：复用 cancel_for_reopen 开门 + 亮满载灯
    - 状态从 2 降回 1/0 时：熄满载灯 + 若门开着则重新关门
    - 大脑（passenger/algorithm）只读 car.weight_state 做决策

重量 IO 读 + ADC 换算已下沉到 executor._poll_weight_once()（脑干层）。
本模块不再直接访问 app.io / app.mapper。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App
from .player import DoorState


class WeightManager:
    """重量三态机管理器（每 app 一个实例）"""

    def __init__(self, app: 'App') -> None:
        self._app = app

    # ===== 三个事件钩子（只读缓存，不触发 IO） =====

    async def on_close_door_starting(self, car_id: int) -> bool:
        """H1: 关门动作开始时触发（executor 收到 CLOSE_DOOR 前）

        car.weight_state 由 executor 轮询器持续更新，这里只读缓存。
        返回 True=跳过关门（state=2），False=正常关门。
        """
        car = self._app.cars[car_id]
        if car.weight_state == 2:
            await self._handle_overweight(car_id)
            return True
        return False

    async def on_close_door_completed(self, car_id: int) -> None:
        """H2: 关门动作完成时触发

        car.weight_state 已由 executor 轮询器保持最新。
        若关门期间重量飙升到 state=2（轮询器已触发 overweight 回调），
        此处兜底检查。
        """
        car = self._app.cars[car_id]
        if car.weight_state == 2:
            await self._handle_overweight(car_id)

    async def on_door_open_button_pressed(self, car_id: int) -> None:
        """H3: 开门按钮按下时触发

        car.weight_state 已由 executor 轮询器保持最新，无需额外操作。
        保留此钩子以兼容现有调用链。
        """
        # 轮询器已持续更新 weight_state，无需额外 IO 读
        pass

    # ===== executor 回调：状态变化时的副作用 =====

    async def on_overweight(self, car_id: int) -> None:
        """executor 轮询器检测到 state→2 时调用"""
        await self._handle_overweight(car_id)

    async def on_normalized(self, car_id: int) -> None:
        """executor 轮询器检测到 state 从 2 降回 1/0 时调用"""
        car = self._app.cars[car_id]
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

    # ===== 公共查询 =====

    def is_overloaded(self, car_id: int) -> bool:
        """大脑查询：返回 True 表示该车不应响应外呼（state=1 或 2）"""
        car = self._app.cars[car_id]
        return car.weight_state >= 1 if car.max_weight > 0 else False

    # ===== 状态 2 紧急开门（内部） =====

    async def _handle_overweight(self, car_id: int) -> None:
        """状态 2 紧急开门：复用 cancel_for_reopen 流程"""
        car = self._app.cars[car_id]
        # 只有门关着/正在关时才需要开
        if car.door_state in (DoorState.CLOSING, DoorState.CLOSED):
            self._app.executors[car_id].door.cancel_for_reopen()
            from .executor import Action, ActionKind
            await self._app.action_queues[car_id].put(Action(ActionKind.OPEN_DOOR))
        # 亮满载灯
        try:
            await self._app.ui[car_id].set_full_load(True)
        except (KeyError, AttributeError):
            pass
