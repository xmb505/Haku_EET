"""
algorithm.py —— 高层调度算法

设计原则:
    - 算法只看 Car + 待处理召唤列表，输出 Action 列表
    - 完全不知道 IO 地址，不 import io/ 任何东西
    - 无内部状态（每次 decide 都是纯函数），方便测试和热切换

首版只实现 SimpleInternalCall，验证端到端链路。
"""

from abc import ABC, abstractmethod
from typing import Iterable

from .actions import Action, ActionKind
from .player import Car, CarState, Direction, DoorState


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
    首版算法：响应内召。call 命令直接 MOVE 到目标层，不碰门。

    设计原则:
        - call 是测试运动用的，直接到目标即可（不开门、不关门、不 SET_DISPLAY）
        - 门开关是另一个独立子系统（门状态机），不应耦合到调度算法里
        - 让调度算法保持极简：MOVE / 等，下发一个 Action 就等它完成

    行为:
        1. car.state == UNKNOWN → 发 INITIALIZE
        2. 没有任务 → 空
        3. 当前层 < 目标 → MOVE_UP
        4. 当前层 > 目标 → MOVE_DOWN
        5. 当前层 == 目标 → 空（MOVE 完成后由 _on_action_done 清掉 pending）
    """

    name = "simple_internal_call"

    def decide(self, car: Car, pending_calls: Iterable[int]) -> list[Action]:
        # 1. 未初始化
        if car.state == CarState.UNKNOWN:
            return [Action(ActionKind.INITIALIZE)]

        # 2. 故障停用（不主动做事，等故障清除后由 IO 事件或外部触发重 tick）
        if car.state == CarState.FAULT or car.fault.any_active():
            return []

        # [未来计划] 算法作为"大脑"应主动检测门状态再推 MOVE：
        #   if car.door_state != DoorState.CLOSED:
        #       return []  # 门没关好，不安全调度
        # 当前由控制层兜底——OPEN_DOOR 完成时 _handle_algorithm_state_change
        # 返回 True 阻止 _tick 运行。等门关了（/door close 或未来 passenger_flow
        # 关门）CLOSE_DOOK 完成后 _tick 才恢复调度。

        # ★ 门未关好，拒绝派 MOVE（防止 _start_move_up/down 静默失败）
        if car.door_state != DoorState.CLOSED:
            return []

        # 优先使用 call_internal 设的"立即目标" target_floor；
        # 没有再退回 pending_calls 取一个（队列非空）。
        # 用 target_floor 而非 pending[0] 是关键——call 命令刚下时
        # 算法不能挑 pending[0]（之前未完成的）而忽略 call 设的 target。
        target = car.target_floor
        calls = list(pending_calls)
        if target is None:
            if not calls:
                return []
            target = calls[0]

        # ★ 顺路多站停靠：在 pending 中找最近的顺路站作为实际目标
        # 车从 L1 出发 pending=[9, 3, 4] → 先到 L3 再到 L4 最后到 L9
        pos = car.position
        if pos is not None and calls:
            if car.direction == Direction.UP or (car.direction == Direction.IDLE and target > pos):
                # 上行或即将上行：找当前位置之上最近的站
                above = sorted([f for f in calls if f > pos])
                if above:
                    target = above[0]
            elif car.direction == Direction.DOWN or (car.direction == Direction.IDLE and target < pos):
                # 下行或即将下行：找当前位置之下最近的站
                below = sorted([f for f in calls if f < pos], reverse=True)
                if below:
                    target = below[0]
            # 回写 target_floor：executor 用 car.target_floor 判断何时刹车，
            # 不回写会导致车跳过中间站直奔原始远端目标
            if car.target_floor != target:
                car.target_floor = target

        # 已到达目标层：空（让 _on_action_done 在 MOVE 完成时清掉 pending + target_floor）
        if car.position == target:
            return []

        # 需要上行
        if car.position is not None and car.position < target:
            return [Action(ActionKind.MOVE_UP)]

        # 需要下行
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
