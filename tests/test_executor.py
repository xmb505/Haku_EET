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
    display = DisplayEncoder(DISPLAY_PATH, io=io, mapper=mapper)
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

    # 触发 bottom_limit_1（下行方向触底 → 反向基站段低速上行）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    # 反向：up_contactor=1, 基站段低速, 电机保持
    # 基-客分段：反冲后第一层(基站段) 全程低速
    # 出临界点(L0)后再用客运段减速(≥2 高速，=1 低速)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1  # 反向
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1  # 基站段低速
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
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
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

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
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

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
    # 4 楼时 remaining=1，应切低速（dist=1），保持电机运行
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1

    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))  # 4→5
    await asyncio.sleep(0.02)

    assert car.position == 5
    assert car.direction == Direction.IDLE
    # 接触器清零
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0
    # 停车后保持全刹(7档),防止过冲,直到下一次 MOVE 才释放
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

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

    # DoorController manages its own door_open_done listener via IOClient dispatch
    db = mapper.addr_input('door_open_done', 1)
    i_addr = mapper.db_to_i(db)
    io.simulate_input(i_addr, 1)
    await asyncio.sleep(0.02)

    assert car.door_state == DoorState.OPEN

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

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
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

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
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

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

    # 触 top_limit_1 → 反向基站段低速下行（不刹车）
    # 基-客分段：反冲后第一层(基站段)全程低速
    await executor.on_io_event(i_to_event(mapper, 'top_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('down_contactor', 1)) == 1  # 反向
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1  # 基站段低速
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
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


@pytest.mark.asyncio
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_motor_stop_does_not_touch_brakes(setup):
    """验证重构后 stop() 不动刹车状态（手动刹车档位不被吃掉）

    场景:手动模式设了 brake_level=5,之后调 motor.stop()
    期望:接触器清零 + motor_start=0,但 brake_1/2/3 保持非 0
    """
    car, io, mapper, display, executor = setup
    # 模拟手动模式设了刹车档位 5 (=101,b1=1, b2=0, b3=1)
    await executor.motor.set_brake_level(5)
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    # 模拟手动停电机(空格或退出手动)
    await executor.motor.start(high_speed=True, direction='up')
    await executor.motor.stop()

    # stop 后接触器清零 + 电机断电
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 0
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 0
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 刹车状态保持（不被 stop 吃掉）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    # 显式 release_brakes 才会清
    await executor.motor.release_brakes()
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 0


@pytest.mark.asyncio
async def test_release_brakes_called_on_move_start(setup):
    """验证 _start_move_up/down 启动前调用 release_brakes（确保刹车释放让电机驱动）"""
    car, io, mapper, display, executor = setup
    queue = ActionQueue()
    car.state = CarState.READY
    car.position = 3
    car.target_floor = 5

    # 先人为设个非零刹车状态（模拟手动模式残留）
    await executor.motor.set_brake_level(3)
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.MOVE_UP))
    await asyncio.sleep(0.02)

    # 启动 MOVE_UP 后,_start_move_up 应释放刹车(让电机能驱动)
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 0
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 0
    # 接触器 + 电机正常
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('motor_start', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_direction_indicator_during_move(setup):
    """验证上下行启动时点亮对应方向灯,中间层更新显示,到达 target 后灭灯

    /car 1 call 4 场景:1→2→3→4
    - 启动:up_indicator=1, down_indicator=0
    - 经过 2 楼:display 应显示 2
    - 经过 3 楼:display 应显示 3
    - 到达 4 楼:up_indicator=0, down_indicator=0
    """
    car, io, mapper, display, executor = setup
    queue = ActionQueue()
    car.state = CarState.READY
    car.position = 1
    car.target_floor = 4

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.MOVE_UP))
    await asyncio.sleep(0.02)

    # 启动:上行灯亮,下行灯灭
    assert io.get_output(mapper.addr_output('up_indicator', 1)) == 1
    assert io.get_output(mapper.addr_output('down_indicator', 1)) == 0
    assert car.display == 1  # 启动时还没移动,显示原位置

    # 经过 2 楼:display 应更新到 2 (中间层)
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))
    await asyncio.sleep(0.02)
    assert car.position == 2
    assert car.display == 2  # ← 关键:中间层也要更新
    # 灯保持
    assert io.get_output(mapper.addr_output('up_indicator', 1)) == 1

    # 经过 3 楼:display 应更新到 3
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))
    await asyncio.sleep(0.02)
    assert car.position == 3
    assert car.display == 3

    # 到达 4 楼 (target):display=4 + 两个灯都清
    await executor.on_io_event(i_to_event(mapper, 'level_up', 1))
    await asyncio.sleep(0.02)
    assert car.position == 4
    assert car.display == 4
    assert car.state == CarState.READY
    assert car.direction == Direction.IDLE
    assert io.get_output(mapper.addr_output('up_indicator', 1)) == 0
    assert io.get_output(mapper.addr_output('down_indicator', 1)) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_direction_indicator_during_move_down(setup):
    """下行方向灯验证:MOVE_DOWN 后 down_indicator=1"""
    car, io, mapper, display, executor = setup
    queue = ActionQueue()
    car.state = CarState.READY
    car.position = 5
    car.target_floor = 2

    task = asyncio.create_task(executor.run_loop(queue))
    await queue.put(Action(ActionKind.MOVE_DOWN))
    await asyncio.sleep(0.02)

    assert io.get_output(mapper.addr_output('up_indicator', 1)) == 0
    assert io.get_output(mapper.addr_output('down_indicator', 1)) == 1

    # 经过 4 楼
    await executor.on_io_event(i_to_event(mapper, 'level_down', 1))
    await asyncio.sleep(0.02)
    assert car.position == 4
    assert car.display == 4

    # 到达 2 楼,两个灯都清
    await executor.on_io_event(i_to_event(mapper, 'level_down', 1))
    await executor.on_io_event(i_to_event(mapper, 'level_down', 1))
    await asyncio.sleep(0.02)
    assert car.position == 2
    assert io.get_output(mapper.addr_output('up_indicator', 1)) == 0
    assert io.get_output(mapper.addr_output('down_indicator', 1)) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_base_segment_keeps_low_speed_on_target_in_base(setup):
    """基-客分段：target 落在基站段内（init down 1）→ 全程低速

    配置：init_direction=down, bottom_base=0, target=1
    路线：触底限位 → 反向低速上行 → L0+1 完美平层 = target=L1 → 完成
    期望：reverse 后是低速；第一个平层脉冲(L0+1=target)直接完成
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=1, base=0 → 仅需一次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('up_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0

    # 唯一一次完美平层（L0→L1=target）→ 应完成
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)

    assert car.state == CarState.READY
    assert car.position == 1
    assert car.display == 1
    # 刹车 7 档全开（到站刹车）
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_init_passenger_segment_uses_normal_decel(setup):
    """基-客分段：target 落在客运段（init down 5）→ 基站段低速，客运段切高速再减速

    配置：init_direction=down, bottom_base=0, target=5
    路线：触底限位 → 反向低速到 L0（基站段）→ 临界点
          L0→L4 高速（remaining≥2），L4→L5 低速（remaining=1）
    """
    car, io, mapper, display, executor = setup
    executor.init_direction = 'down'
    executor.bottom_base_floor = 0
    executor.top_base_floor = 11
    queue = ActionQueue()

    task = asyncio.create_task(executor.run_loop(queue))
    # target=5, base=0 → 5 次完美平层计数
    await queue.put(Action(ActionKind.INITIALIZE, floor=5))
    await asyncio.sleep(0.02)

    # 触底限位 → 反向上行（基站段，低速）
    await executor.on_io_event(i_to_event(mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1

    # 第一个完美平层 = L0→L1 = 出基站段临界点
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    # 出基站段：应切高速（客运段 remaining=|5-1|=4 ≥ 2）
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 0
    assert car.position == 1

    # L1→L2 高速，remaining=3 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 2

    # L2→L3 高速，remaining=2 ≥ 2，保持高速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 1
    assert car.position == 3

    # L3→L4 高速，remaining=1 → 客运段切低速
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert io.get_output(mapper.addr_output('low_speed_contactor', 1)) == 1
    assert io.get_output(mapper.addr_output('high_speed_contactor', 1)) == 0
    assert car.position == 4

    # L4→L5 target, 完美平层停
    await fire_perfect_level_pulse(executor, mapper)
    await asyncio.sleep(0.02)
    assert car.state == CarState.READY
    assert car.position == 5
    assert io.get_output(mapper.addr_output('brake_1', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_2', 1)) == 1
    assert io.get_output(mapper.addr_output('brake_3', 1)) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass