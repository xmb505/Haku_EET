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
        top_base_floor=10, bottom_base_floor=1,  # 与 tests 期望一致
    )
    return car, io, mapper, display, executor


def i_to_event(mapper: IOMapper, signal: str, bit: int, car_id: int = 1) -> IOEvent:
    db = mapper.addr_input(signal, car_id)
    i_addr = mapper.db_to_i(db)
    return IOEvent(i_addr=i_addr, bit=bit)


async def fire_perfect_level_pulse(executor: ActionExecutor, mapper: IOMapper,
                                   car_id: int = 1) -> None:
    """模拟一次完整的"完美平层"脉冲：上升沿 (1,1) + 下降沿 (0,0)

    真实硬件行为：电梯经过一层平层区，level_up 和 level_down 同时=1 持续几百毫秒，
    然后同时回 0。executor 的上升沿触发 step，下降沿 reset 以接受下一个上升沿。
    """
    addr_up = mapper.db_to_i(mapper.addr_input('level_up', car_id))
    addr_down = mapper.db_to_i(mapper.addr_input('level_down', car_id))
    # 上升沿：两个同时=1
    await executor.on_io_event(IOEvent(i_addr=addr_up, bit=1))
    await executor.on_io_event(IOEvent(i_addr=addr_down, bit=1))
    # 下降沿：两个同时=0
    await executor.on_io_event(IOEvent(i_addr=addr_up, bit=0))
    await executor.on_io_event(IOEvent(i_addr=addr_down, bit=0))


@pytest.mark.asyncio
async def test_initialize_down_triggers_bottom_limit(setup):
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = -1  # base=-1, target=1 → 真正需要反向计数
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 全速下行
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 0

    # 触发 bottom_limit_1（下行方向触底 → 反向全速上行）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    # 反向：up_contactor=1, 高速保持, 电机保持
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1  # 反向
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1  # 保持高速
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1  # 电机保持
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 0  # 不刹车
    assert car.position == -1  # 从底站开始

    # 模拟完美平层计数：-1 → 0 → 1（target=1）
    # 每次跨层 = 一次完整脉冲（上升沿+下降沿）
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.position == 0
    assert car.state == CarState.UNKNOWN  # 还没到 target=1

    # 第二次完美平层 → 0→1 = target → complete
    await fire_perfect_level_pulse(executor, mapper)
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
async def test_initialize_up_triggers_top_limit(setup):
    """up 方向：全速上行 → 触 top_limit_1 → 反向全速下行 → 逐层计数→ READY"""
    car, io, mapper, display, executor = setup
    executor.init_direction = 'up'
    executor.top_base_floor = 10
    executor.bottom_base_floor = 1
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=10 → 从 10 开始每层 -1 直到 1
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 全速上行
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1

    # 触 top_limit_1 → 反向全速下行（不刹车）
    await executor.on_io_event(i_to_event(mapper, 'top_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 1  # 反向
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1  # 保持高速
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1  # 电机保持

    # 逆行计数：10→9→8→...→1（9 次完美平层，target=1）
    # 每次跨层 = 一次完整平层脉冲（上升沿+下降沿）
    for i in range(9):
        await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1  # 最终到 1 楼

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass