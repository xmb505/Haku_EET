"""
test_executor.py —— 硬件层 FSM 单测

全部用 simulate IO 验证：
    - Action 被正确展开为 IO 操作
    - 传感器信号触发 action 完成
    - Car 状态正确更新
"""
import asyncio
from pathlib import Path

import pytest

from core.actions import Action, ActionKind, ActionQueue
from core.display import DisplayEncoder
from core.executor import ActionExecutor
from core.io_client import IOClient, IOEvent
from core.io_mapper import IOMapper
from core.player import Car, CarState, Direction, DoorState

DISPLAY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'display_config.yaml'
IO_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'io_config.yaml'


@pytest.fixture
def setup():
    car = Car(car_id=1)
    io = IOClient(simulate=True, debug=False)
    mapper = IOMapper(IO_CONFIG_PATH)
    display = DisplayEncoder(DISPLAY_PATH)
    executor = ActionExecutor(
        car=car, io=io, mapper=mapper, display=display,
        car_id=1, init_direction='up',
    )
    return car, io, mapper, display, executor


def i_to_event(mapper: IOMapper, signal: str, bit: int, car_id: int = 1) -> IOEvent:
    db = mapper.addr_input(signal, car_id)
    i_addr = mapper.db_to_i(db)
    return IOEvent(i_addr=i_addr, bit=bit)


@pytest.mark.asyncio
async def test_initialize_down_triggers_bottom_limit(setup):
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.INITIALIZE))
    await asyncio.sleep(0.02)

    # 全速下行
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 0

    # 触发 1 限位 → 全刹车减速（仍不下行）
    await executor.on_io_event(i_to_event(mapper, 'top_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0

    # 等完美平层（level_up & level_down 同时为 1）
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))
    await executor.on_io_event(i_to_event(mapper, 'level_down', 1))
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_initialize_2_limit_triggers_emergency_stop(setup):
    """触到 2 限位（坠机限位）= 紧急停止 + 故障状态"""
    car, io, mapper, display, executor = setup
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.INITIALIZE))
    await asyncio.sleep(0.02)

    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_2', 1))
    await asyncio.sleep(0.02)

    assert car.state == CarState.FAULT
    # 所有输出应被清零
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_move_up_completes_on_level_up(setup):
    car, io, mapper, display, executor = setup
    queue = ActionQueue()
    car.state = CarState.READY
    car.position = 3
    car.target_floor = 5

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.MOVE_UP))
    await asyncio.sleep(0.02)

    # IO 已发出
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 0
    assert car.direction == Direction.UP

    # 触发上平层 2 次（3→4→5）→ 到达 5 楼
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))  # 3→4
    await asyncio.sleep(0.02)
    # 4 楼时 remaining=1，应触发 brake_3 + 关闭电机
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0

    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))  # 4→5
    await asyncio.sleep(0.02)

    assert car.position == 5
    assert car.direction == Direction.IDLE
    # 接触器清零
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_open_door_completes_on_door_open_done(setup):
    car, io, mapper, display, executor = setup
    queue = ActionQueue()
    car.state = CarState.READY
    car.position = 5
    car.door_state = DoorState.CLOSED

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.OPEN_DOOR))
    await asyncio.sleep(0.02)

    assert io.get_output(mapper.addr_output('door_open_relay', 1)) == 1
    assert io.get_output(mapper.addr_output('door_close_relay', 1)) == 0
    assert car.door_state == DoorState.OPENING

    await executor.on_io_event(i_to_event(mapper, 'door_open_done', 1))
    await asyncio.sleep(0.02)

    assert car.door_state == DoorState.OPEN

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_set_display_immediate(setup):
    car, io, mapper, display, executor = setup
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.SET_DISPLAY, floor=7))
    await asyncio.sleep(0.02)

    # 7 楼 → 笔画 a, b, c（没有 d/e/f/g）
    assert io.get_output(mapper.addr_output('segment_a', 1)) == 1
    assert io.get_output(mapper.addr_output('segment_b', 1)) == 1
    assert io.get_output(mapper.addr_output('segment_c', 1)) == 1
    assert io.get_output(mapper.addr_output('segment_d', 1)) == 0
    assert io.get_output(mapper.addr_output('segment_g', 1)) == 0

    # Car.display 更新
    assert car.display == 7

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_fault_signal_updates_car(setup):
    car, io, mapper, display, executor = setup

    # 触发超重
    await executor.on_io_event(i_to_event(mapper, 'overload', 1))
    assert car.fault.overload is True

    # 触发光幕
    await executor.on_io_event(i_to_event(mapper, 'light_curtain', 1))
    assert car.fault.light_curtain is True

    # 复位光幕
    await executor.on_io_event(i_to_event(mapper, 'light_curtain', 0))
    assert car.fault.light_curtain is False


@pytest.mark.asyncio
async def test_action_done_callback(setup):
    car, io, mapper, display, executor = setup
    queue = ActionQueue()

    completed = []

    async def on_done(action):
        completed.append(action)

    executor.on_action_done = on_done
    car.state = CarState.READY
    car.position = 3
    car.target_floor = 5

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.MOVE_UP))
    await asyncio.sleep(0.02)

    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))  # 3→4
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))  # 4→5
    await asyncio.sleep(0.02)

    # 回调被触发，传入刚完成的 action
    assert len(completed) == 1
    assert completed[0].kind == ActionKind.MOVE_UP

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_initialize_up_triggers_bottom_limit(setup):
    """默认 up 方向：全速上行 → 触 1 限位 → 减速 → 完美平层 → READY"""
    car, io, mapper, display, executor = setup
    # 默认 init_direction = 'up'
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.INITIALIZE))
    await asyncio.sleep(0.02)

    # 全速上行
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1

    # 触发 bottom_limit_1 → 全力刹车减速（不停车）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0

    # 等完美平层
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))
    await executor.on_io_event(i_to_event(mapper, 'level_down', 1))
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass