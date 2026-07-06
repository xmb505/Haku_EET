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


def test_per_car_io_write_isolation():
    """验证每部电梯有自己的 io_write(独立 write_buffer + flush),避免 6 部共享拥堵

    共享 self.io(只读 WS),写入走各自的 io_write[cid]。
    """
    a = App(
        config_path=CONFIG_PATH,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    # 默认 6 部车都有 io_write
    for cid in a.car_ids:
        assert cid in a.io_write, f'car {cid} 缺少独立 io_write'
        assert a.io_write[cid] is not a.io, f'car {cid} 的 io_write 应该是独立实例,不是 self.io'

    # 各 io_write 的 write_buffer 独立(不是同一对象)
    buf_ids = [id(a.io_write[cid]._write_buffer) for cid in a.car_ids]
    assert len(set(buf_ids)) == len(a.car_ids), '各 io_write 的 _write_buffer 应独立'

    # 各 io_write 不连 WS(只写模式)
    for cid in a.car_ids:
        assert a.io_write[cid].ws_url is None
        assert a.io_write[cid]._ws_task is None

    # 共享 input/output cache(self.io 的更新能被 io_write 看到)
    for cid in a.car_ids:
        assert a.io_write[cid]._input_cache is a.io._input_cache
        assert a.io_write[cid]._output_cache is a.io._output_cache

    # 写入通过 io_write[1] 后,self.io.get_output() 也能读到(共享 _output_cache)
    import asyncio
    addr = a.mapper.addr_output('up_contactor', 1)
    asyncio.run(a.io_write[1].set_many({addr: 1}))
    assert a.io.get_output(addr) == 1


@pytest.mark.asyncio
async def test_call_internal_while_passing_floor(app: App):
    """call_internal 在车移动中经过目标楼层时不应静默丢弃合法召唤

    复现 car2 bug：车正在从 L8 下行到 L2 时 call_internal(5)，
    旧代码会因 position==5 拦截，新代码应记录 5（因为 pending 非空）。
    """
    from core.player import CarState

    # 场景 A：车正在从 L8 下行到 L2（pending=[2]），call_internal(5)
    app.cars[1].state = CarState.READY
    app.cars[1].position = 8
    app.cars[1].target_floor = 2
    app.pending_calls[1] = [2]

    # 模拟车经过 L5（在这一瞬时 call_internal(5)）
    app.cars[1].position = 5
    await app.call_internal(5, car_id=1)

    # 修复后：5 必须进入 pending（因为车有未完成任务，position==floor 不应拦截）
    assert 5 in app.pending_calls[1], \
        f'修复后期望 pending 含 5，实际 {app.pending_calls[1]}'
    assert app.pending_calls[1] == [2, 5]

    # 场景 B：车空闲在 L5（pending=[]），call_internal(5) — 应被拦截避免 stale
    app.cars[1].position = 5
    app.pending_calls[1] = []
    app.cars[1].target_floor = None
    await app.call_internal(5, car_id=1)

    # 验证：5 没被加入 pending（空闲时拦截仍然有效）
    assert 5 not in app.pending_calls[1], \
        f'空闲时 call_internal(5) 应被拦截，实际 pending={app.pending_calls[1]}'

    # 场景 C：车 idle 但 pending 非空（例如上一次 call 已完成在 pending 中残留），
    # 应仍记录新 call，避免重复拦截
    app.cars[1].position = 5
    app.pending_calls[1] = [8]
    app.cars[1].target_floor = 8
    await app.call_internal(5, car_id=1)
    assert 5 in app.pending_calls[1], \
        f'pending 非空时 call_internal(5) 应记录，实际 {app.pending_calls[1]}'


@pytest.mark.asyncio
async def test_batch_call_scenario_replays_user_bug(app: App):
    """端到端复现：car2 在批量 init+call 场景下应正确停在 L5

    用户报告场景（car2）：
        /car all init down 2
        /car all call 9,8,7,6,5,4  → car2 → 8
        /car all call 1,2,3,4,5,6  → car2 → 2
        /car all call 6,5,4,3,2,1  → car2 → 5

    旧代码：call 5 在车经过 L5 时被 position==floor 拦截，car2 最终停在 L2。
    修复后：call 5 进入 pending，car2 完成 8→2→5 序列，最终停在 L5。
    """
    # 1. /car all init down 2（对 car2 单独做 init，其他车无关）
    await app.reset(direction='down', target_floor=2, car_id=2)
    await asyncio.sleep(2.0)
    assert app.cars[2].state == CarState.READY
    assert app.cars[2].position == 2

    # 2. /car all call 9,8,7,6,5,4 → car2 → call_internal(8)
    await app.call_internal(8, car_id=2)

    # 等车启动离开 L2（VPLC floor_travel_time=0.4s，留 0.5s 余量）
    await asyncio.sleep(0.5)

    # 3. /car all call 1,2,3,4,5,6 → car2 → call_internal(2)
    await app.call_internal(2, car_id=2)

    # 4. /car all call 6,5,4,3,2,1 → car2 → call_internal(5)
    # 此调用可能在车下行经过 L5 时执行。修复后 5 应进入 pending。
    await app.call_internal(5, car_id=2)

    # 等所有任务完成：8→2→5 共 9 楼层 × 0.4s = 3.6s，再加 4s 余量
    await asyncio.sleep(8.0)

    # 验证 car2 最终在 L5，pending 清空，未触发 FAULT
    assert app.cars[2].state != CarState.FAULT, \
        f'不应触发 emergency，但 car2 FAULT: {app.cars[2].fault}'
    assert app.cars[2].position == 5, \
        f'修复后期望 car2 在 L5，实际 L{app.cars[2].position}（旧 bug 表现为 L2）'
    assert app.pending_calls[2] == [], \
        f'期望 pending 清空，实际 {app.pending_calls[2]}'
    assert app.cars[2].target_floor is None


# ===== change_internal 测试 =====

@pytest.mark.asyncio
async def test_change_not_running(app: App):
    """空闲时 change → not_running"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 3
    app.cars[1].target_floor = 9

    result = await app.change_internal(6, car_id=1)
    assert result == 'not_running'
    # target_floor 不应被修改
    assert app.cars[1].target_floor == 9


@pytest.mark.asyncio
async def test_change_accepted(app: App):
    """MOVE_UP pos=3 target=9 change=6 → accepted, target 改为 6, pending 清空"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 3
    app.cars[1].target_floor = 9
    app.pending_calls[1] = [9]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.change_internal(6, car_id=1)
    assert result == 'accepted'
    assert app.cars[1].target_floor == 6
    assert app.pending_calls[1] == []


@pytest.mark.asyncio
async def test_change_rejected_too_late(app: App):
    """MOVE_UP pos=5 target=9 change=6 → rejected（已过 5 楼）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 5
    app.cars[1].target_floor = 9
    app.pending_calls[1] = [9]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.change_internal(6, car_id=1)
    assert result == 'rejected'
    # target_floor 不应被修改
    assert app.cars[1].target_floor == 9
    assert app.pending_calls[1] == [9]


@pytest.mark.asyncio
async def test_change_rejected_extends(app: App):
    """MOVE_UP pos=3 target=4 change=6 → rejected（延长行程，应用 call）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 3
    app.cars[1].target_floor = 4
    app.pending_calls[1] = [4]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.change_internal(6, car_id=1)
    assert result == 'rejected'
    assert app.cars[1].target_floor == 4


@pytest.mark.asyncio
async def test_change_down_accepted(app: App):
    """MOVE_DOWN pos=8 target=2 change=5 → accepted"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 8
    app.cars[1].target_floor = 2
    app.pending_calls[1] = [2]
    app.executors[1].current_action = Action(ActionKind.MOVE_DOWN)

    result = await app.change_internal(5, car_id=1)
    assert result == 'accepted'
    assert app.cars[1].target_floor == 5
    assert app.pending_calls[1] == []


@pytest.mark.asyncio
async def test_change_down_rejected_too_late(app: App):
    """MOVE_DOWN pos=5 target=2 change=5 → rejected（已过 5 楼）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 5
    app.cars[1].target_floor = 2
    app.pending_calls[1] = [2]
    app.executors[1].current_action = Action(ActionKind.MOVE_DOWN)

    result = await app.change_internal(5, car_id=1)
    assert result == 'rejected'
    assert app.cars[1].target_floor == 2


# ===== fireman 测试 =====

@pytest.mark.asyncio
async def test_fireman_not_moving(app: App):
    """不在运行 → called"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 5

    result = await app.fireman(3, car_id=1)
    assert result['status'] == 'called'
    assert 3 in app.pending_calls[1]


@pytest.mark.asyncio
async def test_fireman_direct_change(app: App):
    """MOVE_UP pos=4→10 fireman=6 → changed（场景A：顺向刹得住）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 4
    app.cars[1].target_floor = 10
    app.pending_calls[1] = [10]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.fireman(6, car_id=1)
    assert result['status'] == 'changed'
    # change_internal 已清空 pending + 改了 target
    assert app.cars[1].target_floor == 6
    assert app.pending_calls[1] == []


@pytest.mark.asyncio
async def test_fireman_waypoint(app: App):
    """MOVE_UP pos=4→10 fireman=5 → waypoint（场景B：顺向刹不住，先到6再倒车）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 4
    app.cars[1].target_floor = 10
    app.pending_calls[1] = [10, 8]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.fireman(5, car_id=1)
    assert result['status'] == 'waypoint'
    assert result['waypoint'] == 6
    # change_internal(6) 清空 pending 改 target
    assert app.cars[1].target_floor == 6
    # pending 被 change 清空后追加了 fireman floor
    assert app.pending_calls[1] == [5]


@pytest.mark.asyncio
async def test_fireman_queued_no_waypoint(app: App):
    """MOVE_UP pos=9→10 fireman=4 → queued（场景C：无合法中间站）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 9
    app.cars[1].target_floor = 10
    app.pending_calls[1] = [10]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.fireman(4, car_id=1)
    assert result['status'] == 'queued'
    # target_floor 不动（change 未调用）
    assert app.cars[1].target_floor == 10
    # 队列清空只剩 fireman
    assert app.pending_calls[1] == [4]


@pytest.mark.asyncio
async def test_fireman_down_direct(app: App):
    """MOVE_DOWN pos=7→2 fireman=3 → changed（场景D：顺向刹得住）"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 7
    app.cars[1].target_floor = 2
    app.pending_calls[1] = [2]
    app.executors[1].current_action = Action(ActionKind.MOVE_DOWN)

    result = await app.fireman(3, car_id=1)
    assert result['status'] == 'changed'
    assert app.cars[1].target_floor == 3
    assert app.pending_calls[1] == []


@pytest.mark.asyncio
async def test_fireman_same_target(app: App):
    """fireman floor == target → noop"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 4
    app.cars[1].target_floor = 6
    app.pending_calls[1] = [6]
    app.executors[1].current_action = Action(ActionKind.MOVE_UP)

    result = await app.fireman(6, car_id=1)
    assert result['status'] == 'noop'
    # 一切不变
    assert app.cars[1].target_floor == 6
    assert app.pending_calls[1] == [6]