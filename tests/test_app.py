"""
test_app.py —— App 集成测试（simulate 模式，端到端验证）
"""
import asyncio
from pathlib import Path

import pytest

from core.actions import Action, ActionKind
from core.app import App
from core.console import Console
from core.io_client import IOEvent
from core.player import CarState, Direction, DoorState

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'config.yaml'
IO_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'io_config.yaml'
DISPLAY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'display_config.yaml'


@pytest.fixture
async def app():
    a = App(
        config_path=CONFIG_PATH,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    await a.start()
    yield a
    await a.stop()


def i_event(mapper, signal: str, bit: int, car_id: int = 1) -> IOEvent:
    db = mapper.addr_input(signal, car_id)
    return IOEvent(i_addr=mapper.db_to_i(db), bit=bit)


@pytest.mark.asyncio
async def test_app_starts_idle_without_init(app: App):
    """启动后不自动 INITIALIZE（避免撞 2 限位），等待用户手动命令"""
    await asyncio.sleep(0.05)
    assert app.car.state == CarState.UNKNOWN
    assert app.car.direction == Direction.IDLE
    # 所有接触器/电机不应被拉起
    assert app.io.get_output(app.mapper.addr_output('up_contactor', 1)) == 0
    assert app.io.get_output(app.mapper.addr_output('high_speed_contactor', 1)) == 0
    assert app.io.get_output(app.mapper.addr_output('motor_start', 1)) == 0


@pytest.mark.asyncio
async def test_initialize_end_to_end(app: App):
    """完整跑通：手动 init（down 方向）→ 全速下行 → 触 1 限位 → 全刹车减速 → 等完美平层 → READY"""
    await asyncio.sleep(0.05)
    from core.actions import Action, ActionKind
    app.executor.init_direction = 'down'
    app.executor.top_base_floor = 10
    app.executor.bottom_base_floor = 1  # 与测试期望一致
    await app.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.05)

    # 1. 触发 bottom_limit_1（下行方向等底限位）
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.05)

    # 还没完成：等完美平层
    assert app.car.state == CarState.UNKNOWN

    # 2. 触发 level_up & level_down（完美平层） → 完成 INITIALIZE
    await app.executor.on_io_event(i_event(app.mapper, 'level_up', 1))
    await app.executor.on_io_event(i_event(app.mapper, 'level_down', 1))
    await asyncio.sleep(0.05)

    assert app.car.state == CarState.READY
    assert app.car.position == 1  # down 方向基站=1
    assert app.car.display == 1
    assert app.io.get_output(app.mapper.addr_output('segment_b', 1)) == 1
    assert app.io.get_output(app.mapper.addr_output('segment_c', 1)) == 1


@pytest.mark.asyncio
async def test_initialize_2_limit_emergency_stop(app: App):
    """触到 2 限位 = 坠机，紧急停止 + 故障状态"""
    await asyncio.sleep(0.05)
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_2', 1))
    await asyncio.sleep(0.05)

    assert app.car.state == CarState.FAULT
    # 所有接触器应被清零
    assert app.io.get_output(app.mapper.addr_output('motor_start', 1)) == 0
    assert app.io.get_output(app.mapper.addr_output('up_contactor', 1)) == 0


@pytest.mark.asyncio
async def test_call_internal_triggers_move(app: App):
    """内召 → 算法发 MOVE_UP → executor 拉上行接触器"""
    await asyncio.sleep(0.05)
    from core.actions import Action, ActionKind
    app.executor.init_direction = 'down'
    app.executor.top_base_floor = 10
    app.executor.bottom_base_floor = 1
    await app.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.05)
    # 完成 INITIALIZE（down 方向等 bottom_limit）
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_1', 1))
    await app.executor.on_io_event(i_event(app.mapper, 'level_up', 1))
    await app.executor.on_io_event(i_event(app.mapper, 'level_down', 1))
    await asyncio.sleep(0.05)
    assert app.car.state == CarState.READY

    # 内召 5 楼（从 1 楼向上）
    await app.call_internal(5)
    await asyncio.sleep(0.05)

    # executor 应该已经在拉上行接触器
    assert app.io.get_output(app.mapper.addr_output('up_contactor', 1)) == 1
    assert app.io.get_output(app.mapper.addr_output('high_speed_contactor', 1)) == 1
    assert app.io.get_output(app.mapper.addr_output('motor_start', 1)) == 1
    assert app.car.direction == Direction.UP
    assert app.car.target_floor == 5


@pytest.mark.asyncio
async def test_move_to_5_floor_open_door(app: App):
    """完整链路：内召 5 → 4 次平层 → 门开 → 门关 → pending 清空"""
    await asyncio.sleep(0.05)
    from core.actions import Action, ActionKind
    app.executor.init_direction = 'down'
    app.executor.top_base_floor = 10
    app.executor.bottom_base_floor = 1
    await app.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.05)
    # 完成 INITIALIZE（down 方向）
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_1', 1))
    await app.executor.on_io_event(i_event(app.mapper, 'level_up', 1))
    await app.executor.on_io_event(i_event(app.mapper, 'level_down', 1))
    await asyncio.sleep(0.05)

    await app.call_internal(5)
    await asyncio.sleep(0.05)

    # 1→5 要经过 4 次上平层（2、3、4、5）
    for _ in range(4):
        await app.executor.on_io_event(i_event(app.mapper, 'level_up', 1))
        await asyncio.sleep(0.02)

    assert app.car.position == 5
    assert app.io.get_output(app.mapper.addr_output('door_open_relay', 1)) == 1

    # 触发门开到位 → door=OPEN → 算法发 CLOSE_DOOR → executor 拉关门继电器
    await app.executor.on_io_event(i_event(app.mapper, 'door_open_done', 1))
    await asyncio.sleep(0.05)

    # 此时 executor 已经在执行 CLOSE_DOOR
    assert app.io.get_output(app.mapper.addr_output('door_close_relay', 1)) == 1
    assert app.io.get_output(app.mapper.addr_output('door_open_relay', 1)) == 0

    # 触发门关到位 → 任务完成，pending 清空
    await app.executor.on_io_event(i_event(app.mapper, 'door_close_done', 1))
    await asyncio.sleep(0.05)

    assert app.car.door_state == DoorState.CLOSED
    assert 5 not in app.pending_calls


@pytest.mark.asyncio
async def test_algorithm_hot_swap(app: App):
    """算法热切换后立即生效"""
    await app.set_algorithm('simple_internal_call')
    assert app.algorithm.name == 'simple_internal_call'


@pytest.mark.asyncio
async def test_reload_config(app: App, tmp_path):
    """reload 后 config 重新生效（直接改 yaml 文件再 reload）"""
    # 写一个临时 config，初始化方向改成 up
    new_cfg = tmp_path / 'config.yaml'
    new_cfg.write_text('''
io2http:
  http_url: http://192.168.1.201:8080/gpio
  ws_url: ws://192.168.1.201:8081/
building:
  min_floor: 1
  max_floor: 10
elevator:
  car_id: 1
  initialization_direction: up
algorithm:
  name: simple_internal_call
logging:
  level: INFO
''', encoding='utf-8')

    app.config_path = new_cfg
    await app.reload()
    assert app.executor.init_direction == 'up'


@pytest.mark.asyncio
async def test_simulate_input_via_app(app: App):
    """通过 /sim input 路径调用（间接通过 io.simulate_input）"""
    # simulate_input 是同步方法，不能 await
    app.io.simulate_input(
        app.mapper.db_to_i(app.mapper.addr_input('overload', 1)),
        1,
    )
    await asyncio.sleep(0.02)
    assert app.car.fault.overload is True


@pytest.mark.asyncio
async def test_status_snapshot(app: App):
    snap = app.status_snapshot()
    assert 'car' in snap
    assert 'algorithm' in snap
    assert 'pending_calls' in snap
    assert snap['simulate'] is True
    assert snap['algorithm'] == 'simple_internal_call'


@pytest.mark.asyncio
async def test_available_algorithms(app: App):
    algos = app.available_algorithms()
    assert 'simple_internal_call' in algos