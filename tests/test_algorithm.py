"""
test_algorithm.py —— 高层算法单测
"""
import pytest

from core.actions import Action, ActionKind
from core.algorithm import ALGORITHM_REGISTRY, SimpleInternalCall, get_algorithm
from core.player import Car, CarState, Direction, DoorState


@pytest.fixture
def algo() -> SimpleInternalCall:
    return SimpleInternalCall()


class TestRegistry:
    def test_simple_registered(self):
        assert 'simple_internal_call' in ALGORITHM_REGISTRY

    def test_get_algorithm(self):
        a = get_algorithm('simple_internal_call')
        assert isinstance(a, SimpleInternalCall)

    def test_unknown_raises(self):
        with pytest.raises(KeyError, match='未知算法'):
            get_algorithm('does_not_exist')


class TestSimpleInternalCall:
    def test_unknown_state_emits_initialize(self, algo):
        car = Car(car_id=1, state=CarState.UNKNOWN)
        actions = algo.decide(car, pending_calls=[])
        assert actions == [Action(ActionKind.INITIALIZE)]

    def test_unknown_state_ignores_calls(self, algo):
        """未初始化时即使有召唤也只发 INITIALIZE（不能动）"""
        car = Car(car_id=1, state=CarState.UNKNOWN)
        actions = algo.decide(car, pending_calls=[5, 7])
        assert actions == [Action(ActionKind.INITIALIZE)]

    def test_fault_emits_empty(self, algo):
        """故障时不主动做事（返回空避免 busy loop），等外部触发重 tick"""
        from core.player import FaultFlags
        car = Car(
            car_id=1, state=CarState.READY, position=1,
            fault=FaultFlags(overload=True),
        )
        actions = algo.decide(car, pending_calls=[5])
        assert actions == []

    def test_no_calls_emits_empty(self, algo):
        car = Car(car_id=1, state=CarState.READY, position=1)
        assert algo.decide(car, pending_calls=[]) == []

    def test_below_target_moves_up(self, algo):
        car = Car(car_id=1, state=CarState.READY, position=3, direction=Direction.IDLE)
        actions = algo.decide(car, pending_calls=[7])
        assert actions == [Action(ActionKind.MOVE_UP)]

    def test_above_target_moves_down(self, algo):
        car = Car(car_id=1, state=CarState.READY, position=8, direction=Direction.IDLE)
        actions = algo.decide(car, pending_calls=[2])
        assert actions == [Action(ActionKind.MOVE_DOWN)]


class TestNoDoorLogic:
    """call 命令不涉及门，任何门状态都不应该影响 dispatch"""

    def test_at_target_door_open_returns_empty(self, algo):
        car = Car(
            car_id=1, state=CarState.READY, position=5,
            door_state=DoorState.OPEN,
        )
        assert algo.decide(car, pending_calls=[5]) == []

    def test_at_target_door_closed_returns_empty(self, algo):
        car = Car(
            car_id=1, state=CarState.READY, position=5,
            door_state=DoorState.CLOSED,
        )
        assert algo.decide(car, pending_calls=[5]) == []

    def test_at_target_door_opening_returns_empty(self, algo):
        car = Car(
            car_id=1, state=CarState.READY, position=5,
            door_state=DoorState.OPENING,
        )
        assert algo.decide(car, pending_calls=[5]) == []


class TestTargetFloorPreferredOverPending:
    """target_floor 是 call 命令的立即目标；不要让它被 pending[0]（旧召唤）挡住"""

    def test_target_floor_wins_when_pending_differs(self, algo):
        car = Car(
            car_id=1, state=CarState.READY, position=10,
            target_floor=1,  # call 1 设的目标
        )
        actions = algo.decide(car, pending_calls=[10, 1])
        # 不应该挑 pending[0]=10（之前没完成的），应该用 target_floor=1
        assert actions == [Action(ActionKind.MOVE_DOWN)]

    def test_no_target_floor_falls_back_to_pending(self, algo):
        car = Car(
            car_id=1, state=CarState.READY, position=2,
            target_floor=None,
        )
        actions = algo.decide(car, pending_calls=[5])
        assert actions == [Action(ActionKind.MOVE_UP)]

    def test_fifo_when_no_target_floor(self, algo):
        car = Car(car_id=1, state=CarState.READY, position=2, target_floor=None)
        actions = algo.decide(car, pending_calls=[5, 8, 3])
        assert actions == [Action(ActionKind.MOVE_UP)]
