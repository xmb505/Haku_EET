"""
test_actions.py —— Action / ActionQueue 单测
"""
import asyncio

import pytest

from core.actions import Action, ActionKind, ActionQueue


class TestActionKind:
    def test_values(self):
        assert ActionKind.INITIALIZE.value == 'initialize'
        assert ActionKind.MOVE_UP.value == 'move_up'
        assert ActionKind.MOVE_DOWN.value == 'move_down'
        assert ActionKind.OPEN_DOOR.value == 'open_door'
        assert ActionKind.CLOSE_DOOR.value == 'close_door'
        assert ActionKind.SET_DISPLAY.value == 'set_display'
        assert ActionKind.RESET_FAULT.value == 'reset_fault'
        assert ActionKind.EMERGENCY_STOP.value == 'emergency_stop'
        assert ActionKind.NOOP.value == 'noop'


class TestAction:
    def test_default_no_floor(self):
        a = Action(kind=ActionKind.MOVE_UP)
        assert a.kind == ActionKind.MOVE_UP
        assert a.floor is None

    def test_with_floor(self):
        a = Action(kind=ActionKind.SET_DISPLAY, floor=5)
        assert a.kind == ActionKind.SET_DISPLAY
        assert a.floor == 5

    def test_frozen(self):
        a = Action(kind=ActionKind.MOVE_UP)
        with pytest.raises(Exception):
            a.kind = ActionKind.MOVE_DOWN  # type: ignore

    def test_equality(self):
        assert Action(ActionKind.MOVE_UP) == Action(ActionKind.MOVE_UP)
        assert Action(ActionKind.MOVE_UP) != Action(ActionKind.MOVE_DOWN)
        assert Action(ActionKind.SET_DISPLAY, floor=5) == Action(ActionKind.SET_DISPLAY, floor=5)
        assert Action(ActionKind.SET_DISPLAY, floor=5) != Action(ActionKind.SET_DISPLAY, floor=6)

    def test_repr_without_floor(self):
        a = Action(kind=ActionKind.MOVE_UP)
        assert repr(a) == 'Action(move_up)'

    def test_repr_with_floor(self):
        a = Action(kind=ActionKind.SET_DISPLAY, floor=5)
        assert repr(a) == 'Action(set_display, floor=5)'


class TestActionQueue:
    def test_empty(self):
        q = ActionQueue()
        assert q.empty()
        assert q.qsize() == 0

    @pytest.mark.asyncio
    async def test_put_get(self):
        q = ActionQueue()
        await q.put(Action(ActionKind.MOVE_UP))
        assert not q.empty()
        assert q.qsize() == 1

        got = await q.get()
        assert got.kind == ActionKind.MOVE_UP
        assert q.empty()

    @pytest.mark.asyncio
    async def test_fifo_order(self):
        q = ActionQueue()
        await q.put(Action(ActionKind.MOVE_UP))
        await q.put(Action(ActionKind.OPEN_DOOR))
        await q.put(Action(ActionKind.SET_DISPLAY, floor=5))

        assert (await q.get()).kind == ActionKind.MOVE_UP
        assert (await q.get()).kind == ActionKind.OPEN_DOOR
        assert (await q.get()).kind == ActionKind.SET_DISPLAY