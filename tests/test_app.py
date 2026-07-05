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
    """init down → 触 bottom_limit → 反向（base==target 立即完成）"""
    await asyncio.sleep(0.05)
    from core.actions import Action, ActionKind
    app.executor.init_direction = 'down'
    app.executor.top_base_floor = 10
    app.executor.bottom_base_floor = 1  # base=1
    await app.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))  # target=1
    await asyncio.sleep(0.05)

    # 触发 bottom_limit_1 → 反向 → base=1=target → 立即完成
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.05)

    assert app.car.state == CarState.READY
    assert app.car.position == 1
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
    # 触发 bottom_limit_1 → 反向完成（base=1=target）
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_1', 1))
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
    """完整链路：内召 5 → 4 次平层 → 直接 complete（call 命令不碰门）→ pending 清空"""
    await asyncio.sleep(0.05)
    from core.actions import Action, ActionKind
    app.executor.init_direction = 'down'
    app.executor.top_base_floor = 10
    app.executor.bottom_base_floor = 1
    await app.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))
    await asyncio.sleep(0.05)
    # 触发 bottom_limit_1 → 反向完成（base=1=target）
    await app.executor.on_io_event(i_event(app.mapper, 'bottom_limit_1', 1))
    await asyncio.sleep(0.05)

    await app.call_internal(5)
    await asyncio.sleep(0.05)

    # 1→5 要经过 4 次上平层（2、3、4、5）
    for _ in range(4):
        await app.executor.on_io_event(i_event(app.mapper, 'level_up', 1))
        await asyncio.sleep(0.02)

    assert app.car.position == 5
    # MOVE_UP 到目标 → 算法发 NOOP → executor 完成 → _on_action_done 清理 pending
    assert 5 not in app.pending_calls[app.current_car_id]
    assert app.car.target_floor is None


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
  top_base_floor: 11
  bottom_base_floor: 0
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
async def test_multi_init_no_emergency(app: App):
    """连续两次 init 不触发 emergency：模拟用户在第一次 init 途中敲第二次 init"""
    app.executor.init_direction = 'up'
    # 第一次 init up 7（目标 7 楼）
    await app.reset(direction='up', target_floor=7, car_id=1)
    await asyncio.sleep(0.05)
    # 还没完成（VPLC 正在往上跑），立刻再 init down 1
    await app.reset(direction='down', target_floor=1, car_id=1)
    # 等完整走完：down→底限位→反转→up→1 楼（2 层 × 0.4s + 余量）
    await asyncio.sleep(2.0)
    # 不应该触发 emergency（状态是 READY 而不是 FAULT）
    assert app.cars[1].state != CarState.FAULT, f'emergency: {app.cars[1].fault}'
    assert app.cars[1].position == 1, f'预期 L1 实际 L{app.cars[1].position}'


@pytest.mark.asyncio
async def test_batch_init(app: App):
    """批量 init 多部轿厢（模拟 /car 1,2,3 init down 5,6,7）"""
    for cid, floor in [(1, 5), (2, 6), (3, 7)]:
        await app.reset(direction='down', target_floor=floor, car_id=cid)
    await asyncio.sleep(5.0)  # 等所有 init 完成（8 层 × 0.4s + 余量）

    for cid in (1, 2, 3):
        assert app.cars[cid].state != CarState.FAULT, \
            f'car{cid} emergency: {app.cars[cid].fault}'
    assert app.cars[1].position == 5
    assert app.cars[2].position == 6
    assert app.cars[3].position == 7


def test_car_ids_loaded_from_config():
    """验证 car_ids 从 config.yaml 加载,默认全跑"""
    a = App(
        config_path=CONFIG_PATH,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    # config.yaml 默认 [1, 2, 3, 4, 5, 6]
    assert a.car_ids == [1, 2, 3, 4, 5, 6]
    # 对应的 car/executor/action_queue 都已实例化
    for cid in a.car_ids:
        assert cid in a.cars
        assert cid in a.executors
        assert cid in a.action_queues
    # car_ids 外的车不应存在
    assert 99 not in a.cars


def test_car_ids_partial(tmp_path):
    """验证 car_ids 配置为子集时,只实例化配置的车"""
    import yaml
    # 复制一份 config,把 car_ids 改成 [2, 4]
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    cfg['elevator']['car_ids'] = [2, 4]
    custom_cfg = tmp_path / 'config.yaml'
    custom_cfg.write_text(yaml.safe_dump(cfg, allow_unicode=True))

    a = App(
        config_path=custom_cfg,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    assert a.car_ids == [2, 4]
    assert 2 in a.cars
    assert 4 in a.cars
    assert 1 not in a.cars
    assert 3 not in a.cars