"""
algorithm.py —— 高层调度算法

设计原则:
    - 算法只看 Car + 待处理召唤列表，输出 Action 列表
    - 完全不知道 IO 地址，不 import io/ 任何东西
    - 无内部状态（每次 decide 都是纯函数），方便测试和热切换

首版只实现 SimpleInternalCall，验证端到端链路。
后续要加集选/节能，只在这个文件加新类，/algo set 切换即可。
"""

from abc import ABC, abstractmethod
from typing import Iterable

from .actions import Action, ActionKind
from .player import Car, CarState, Direction


class ElevatorAlgorithm(ABC):
    """所有算法的基类"""

    name: str = "base"

    @abstractmethod
    def decide(self, car: Car, pending_calls: Iterable[int]) -> list[Action]:
        """
        输入: 当前玩家状态 + 待处理召唤楼层列表
        输出: 动作列表（硬件层会按顺序执行）
        """


class SimpleInternalCall(ElevatorAlgorithm):
    """
    首版算法：响应内召

    行为:
        1. car.state == UNKNOWN → 发 INITIALIZE（启动定位）
        2. 没有任务 → 空
        3. 当前层 < 目标 → MOVE_UP
        4. 当前层 > 目标 → MOVE_DOWN
        5. 当前层 == 目标 且门关 → SET_DISPLAY + OPEN_DOOR
        6. 当前层 == 目标 且门开 → 空（等关门）
    """

    name = "simple_internal_call"

    def decide(self, car: Car, pending_calls: Iterable[int]) -> list[Action]:
        # 1. 未初始化
        if car.state == CarState.UNKNOWN:
            return [Action(ActionKind.INITIALIZE)]

        # 2. 故障停用（不主动做事，等故障清除后由 IO 事件或外部触发重 tick）
        if car.state == CarState.FAULT or car.fault.any_active():
            return []

        calls = list(pending_calls)
        if not calls:
            return []

        # 取最近一个召唤作为目标（FIFO）
        target = calls[0]

        # 5/6. 已到达目标层
        if car.position == target:
            door = car.door_state.value
            if door == 'closed':
                # 门关着 → 显示 + 开门
                return [
                    Action(ActionKind.SET_DISPLAY, floor=target),
                    Action(ActionKind.OPEN_DOOR),
                ]
            if door == 'open':
                # 门已开 → 关门（任务完成后由 _on_action_done 清理 pending）
                return [Action(ActionKind.CLOSE_DOOR)]
            # 门在中间状态（OPENING/CLOSING）→ 等
            return []

        # 3. 需要上行
        if car.position is not None and car.position < target:
            return [Action(ActionKind.MOVE_UP)]

        # 4. 需要下行
        if car.position is not None and car.position > target:
            return [Action(ActionKind.MOVE_DOWN)]

        return []


# 算法注册表（/algo set 时按名字查）
ALGORITHM_REGISTRY: dict[str, type[ElevatorAlgorithm]] = {
    cls.name: cls
    for cls in (SimpleInternalCall,)
}


def get_algorithm(name: str) -> ElevatorAlgorithm:
    """按名字实例化算法"""
    if name not in ALGORITHM_REGISTRY:
        raise KeyError(f'未知算法: {name!r}，可用: {list(ALGORITHM_REGISTRY)}')
    return ALGORITHM_REGISTRY[name]()