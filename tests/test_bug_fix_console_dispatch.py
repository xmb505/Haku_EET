"""完整模拟用户描述的电梯流程: hall_call → door_open → cron → close → cabin → arrive → door_open"""
import asyncio
from pathlib import Path

import pytest

from core.actions import Action, ActionKind
from core.app import App
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


async def _setup_cars_at_l1(app):
    """把 6 部车放到 L1 READY 状态(模拟 init 完成)"""
    for cid in app.car_ids:
        app.cars[cid].position = 1
        app.cars[cid].door_state = DoorState.CLOSED
        await app.reset(direction='down', target_floor=1, car_id=cid)
        app.cars[cid].state = CarState.READY
    # 等所有 init action 处理完(包括残余的 INITIALIZE action)
    await asyncio.sleep(2.5)
    # 设置平层信号为 1（MOVE 完美平层 + OPEN_DOOR 平层校验需要）
    for cid in app.car_ids:
        try:
            app.io.observe_input(app.mapper.addr_input('level_up', cid), 1)
            app.io.observe_input(app.mapper.addr_input('level_down', cid), 1)
        except KeyError:
            pass


@pytest.mark.asyncio
async def test_dispatch_failure_prints_reason_state(app, capsys):
    """dispatch 返回 None 时,pm.on_hall_call 应打印每部车被过滤的原因"""
    # 让所有车都保持 UNKNOWN
    result = app._dispatch_hall_call(1, 'up')
    assert result is None

    if app.pm is not None:
        await app.pm.on_hall_call(1, 'up', 1)

    captured = capsys.readouterr()
    out = captured.err  # passenger 输出已迁移到 stderr
    assert 'no available car' in out
    assert 'car1: state=unknown' in out or 'car1: state=' in out
    print("✓ 诊断输出包含车状态信息")


@pytest.mark.asyncio
async def test_dispatch_failure_prints_reason_door(app, capsys):
    """door != CLOSED 时：快捷路径接管（亮灯+取消cron），或打印诊断"""
    app.cars[1].state = CarState.READY
    app.cars[1].position = 1
    app.cars[1].door_state = DoorState.OPEN

    result = app._dispatch_hall_call(1, 'up')
    assert result is None

    if app.pm is not None:
        await app.pm.on_hall_call(1, 'up', 1)

    captured = capsys.readouterr()
    out = captured.err  # passenger 输出已迁移到 stderr
    # 新行为：快捷路径（门开着 → 亮灯 + cancel cron）
    # 旧行为：诊断输出（门=open）
    assert ('door open' in out or 'keep LED' in out
            or '门=open' in out or 'door=open' in out)
    print("✓ 门已开时正确处理（快捷路径或诊断输出）")


@pytest.mark.asyncio
async def test_reset_clears_door_state(app):
    """reset() 应清 door_state 为 CLOSED(防御性修复)"""
    app.cars[1].door_state = DoorState.OPEN
    assert app.cars[1].door_state == DoorState.OPEN

    await app.reset(direction='down', target_floor=1, car_id=1)
    assert app.cars[1].door_state == DoorState.CLOSED, \
        "Bug 2 防御性修复未生效: reset() 没清 door_state"
    print("✓ reset() 现在会清 door_state 为 CLOSED")


@pytest.mark.asyncio
async def test_full_scenario_after_fix(app, capsys):
    """完整场景:init + usermode + hall_call up @ L1"""
    await _setup_cars_at_l1(app)

    result = await app.set_usermode(True)
    assert result['enabled'] is True
    assert result['blocked'] == []

    if app.pm is not None:
        await app.pm.on_hall_call(1, 'up', 1)

    captured = capsys.readouterr()
    out = captured.err  # passenger 输出已迁移到 stderr
    assert 'car1 at floor, opening' in out or '→ car' in out, \
        f"完整场景应成功派车,实际输出: {out}"
    print(f"✓ 完整场景修复后能正常派车: {out.strip()}")


@pytest.mark.asyncio
async def test_door_open_auto_starts_close_cron(app):
    """Bug 4 修复:门开后自动启动关门 cron(无需等 bit=0)"""
    await _setup_cars_at_l1(app)
    await app.set_usermode(True)

    # 用户按 L1 上行
    await app.pm.on_hall_call(1, 'up', 1)
    # 等开门完成
    await asyncio.sleep(1.5)

    assert app.cars[1].door_state == DoorState.OPEN

    # 验证 cron 已启动
    jobs = list(app.cron._jobs.keys())
    assert any('pm_car1_close_door' in j for j in jobs), \
        f"Bug 4 修复未生效:门开后未自动启动关门 cron,当前 jobs: {jobs}"
    print(f"✓ Bug 4 修复:门开后自动启动关门 cron (jobs: {jobs})")


@pytest.mark.asyncio
async def test_internal_call_arrival_opens_door(app):
    """Bug 3 修复:单次内召到站自动开门(不被 pq 空拦截)"""
    await _setup_cars_at_l1(app)
    await app.set_usermode(True)

    # 用户在 L1 叫车 (hall call)
    await app.pm.on_hall_call(1, 'up', 1)
    await asyncio.sleep(1.5)  # 等开门
    assert app.cars[1].door_state == DoorState.OPEN

    # 用户按内召 5
    await app.pm.on_cabin_button(1, 5)
    print(f"button_cache: {sorted(app.pm._button_cache[1])}")
    assert 5 in app.pm._button_cache[1]

    # 模拟 cron 触发关门(直接 push CLOSE_DOOR,跳过 10s 等待)
    await app.action_queues[1].put(Action(ActionKind.CLOSE_DOOR))
    await asyncio.sleep(1.0)  # 等关门完成
    print(f"car1 door after close: {app.cars[1].door_state.value}")
    assert app.cars[1].door_state == DoorState.CLOSED, \
        f"门应已关闭,实际 {app.cars[1].door_state}"

    # 等车从 L1 到 L5
    await asyncio.sleep(3.0)
    print(f"car1 final: pos={app.cars[1].position} door={app.cars[1].door_state.value}")

    # Bug 3 关键验证: 到达 L5 后门应自动打开
    assert app.cars[1].position == 5, f"car1 应在 L5,实际 L{app.cars[1].position}"
    assert app.cars[1].door_state == DoorState.OPEN, \
        f"Bug 3 修复未生效:单次内召到站 L5 门应自动开,实际 {app.cars[1].door_state}"
    print(f"✓ Bug 3 修复:单次内召到站 L5 门自动打开")


@pytest.mark.asyncio
async def test_user_full_flow_end_to_end(app, capsys):
    """完整用户流程:从按按钮到到达目的地开门"""
    print("\n=== 完整用户流程 ===\n")
    await _setup_cars_at_l1(app)
    await app.set_usermode(True)

    # 1. 用户按 L1 上行
    print("1. 用户按 L1 上行按钮")
    await app.pm.on_hall_call(1, 'up', 1)
    await asyncio.sleep(1.5)
    print(f"   car1 door={app.cars[1].door_state.value}, cron jobs={list(app.cron._jobs.keys())}")
    assert app.cars[1].door_state == DoorState.OPEN

    # 2. 用户按内召 5
    print("2. 用户按内召 5")
    await app.pm.on_cabin_button(1, 5)

    # 3. 取消旧的关门 cron + 立即 push CLOSE_DOOR 模拟 cron 触发
    print("3. 模拟 cron 触发关门")
    await app.cron.cancel('pm_car1_close_door')
    await app.action_queues[1].put(Action(ActionKind.CLOSE_DOOR))
    await asyncio.sleep(1.0)
    print(f"   car1 door after close: {app.cars[1].door_state.value}")
    assert app.cars[1].door_state == DoorState.CLOSED

    # 4. 等车从 L1 到 L5
    print("4. 等车从 L1 到 L5")
    await asyncio.sleep(3.0)
    print(f"   car1 final: pos={app.cars[1].position} door={app.cars[1].door_state.value}")

    # 5. 验证:门在 L5 自动打开
    assert app.cars[1].position == 5
    assert app.cars[1].door_state == DoorState.OPEN
    print("\n✓ 完整流程成功: 用户从 L1 乘梯到 L5,门自动开关")