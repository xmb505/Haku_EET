"""
test_player.py —— player 抽象的单测
"""
import pytest
from core.player import Car, CarState, Direction, DoorState, FaultFlags


class TestEnums:
    def test_car_state_values(self):
        assert CarState.UNKNOWN.value == 'unknown'
        assert CarState.READY.value == 'ready'
        assert CarState.FAULT.value == 'fault'

    def test_direction_values(self):
        assert Direction.IDLE.value == 'idle'
        assert Direction.UP.value == 'up'
        assert Direction.DOWN.value == 'down'

    def test_door_state_values(self):
        assert DoorState.CLOSED.value == 'closed'
        assert DoorState.OPENING.value == 'opening'
        assert DoorState.OPEN.value == 'open'
        assert DoorState.CLOSING.value == 'closing'


class TestFaultFlags:
    def test_default_all_false(self):
        f = FaultFlags()
        assert not f.any_active()

    def test_overload_activates(self):
        f = FaultFlags(overload=True)
        assert f.any_active()

    def test_frozen(self):
        f = FaultFlags()
        with pytest.raises(Exception):
            f.overload = True  # type: ignore


class TestCar:
    def test_defaults(self):
        car = Car(car_id=1)
        assert car.car_id == 1
        assert car.state == CarState.UNKNOWN
        assert car.position is None
        assert car.direction == Direction.IDLE
        assert car.door_state == DoorState.CLOSED
        assert car.target_floor is None
        assert car.display == 1
        assert not car.fault.any_active()

    def test_is_ready(self):
        car = Car(car_id=1, state=CarState.READY)
        assert car.is_ready()

        car.state = CarState.UNKNOWN
        assert not car.is_ready()

        car.state = CarState.READY
        car.fault = FaultFlags(service_mode=True)
        assert not car.is_ready()

    def test_snapshot_keys(self):
        car = Car(car_id=1, state=CarState.READY, position=5)
        snap = car.snapshot()
        assert snap['car_id'] == 1
        assert snap['state'] == 'ready'
        assert snap['position'] == 5
        assert snap['direction'] == 'idle'
        assert snap['door_state'] == 'closed'
        assert snap['display'] == 1
        assert set(snap['fault'].keys()) == {
            'overload', 'service_mode', 'light_curtain',
            'top_limit', 'bottom_limit',
        }

    def test_repr(self):
        car = Car(car_id=1, state=CarState.UNKNOWN)
        assert 'Car(id=1' in repr(car)
        assert 'pos=?' in repr(car)

        car.state = CarState.READY
        car.position = 5
        assert 'pos=L5' in repr(car)