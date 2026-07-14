"""
test_door.py —— /door 命令 + App.control_door 全场景测试

覆盖：
- 非阻塞:control_door 立即返回 dispatched,不卡 REPL
- 后台 task 跟踪 door_open_done/door_close_done
- 错层检测:后台 task 打印 ⚠️
- /debug show door_status 监视开关
- 预检拒绝:未初始化 / 运行中 / 门已开/已关
- 同轿厢互斥
- force 模式
"""
import asyncio
from pathlib import Path

import pytest

from core.app import App
from core.console import Console
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


@pytest.fixture
async def ready_app(app: App):
    """把 car 1 init 到 READY 状态（位置 L1），方便测开门"""
    car_id = 1
    app.cars[car_id].state = CarState.READY
    app.cars[car_id].position = 1
    app.cars[car_id].direction = Direction.IDLE
    app.cars[car_id].door_state = DoorState.CLOSED
    car_lock_i = app.mapper.addr_input('car_door_lock', car_id)
    app.io.observe_input(car_lock_i, 1)
    floor_lock_i = app.mapper.addr_input('floor_door_lock_1', car_id)
    app.io.observe_input(floor_lock_i, 1)
    yield app


async def _wait_for_busy_release(app: App, car_id: int, timeout: float = 0.5):
    """等待后台任务完成(释放 _door_busy[car_id])"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if not app._door_busy[car_id]:
            return True
        await asyncio.sleep(0.02)
    return False


# ===== 预检拒绝 =====

class TestRejectedPrechecks:
    @pytest.mark.asyncio
    async def test_uninitialized_requires_force(self, app: App):
        result = await app.control_door(1, 'open', force=False)
        assert result['status'] == 'rejected'
        assert '未初始化' in result['message']

    @pytest.mark.asyncio
    async def test_uninitialized_force_succeeds(self, app: App):
        result = await app.control_door(1, 'open', force=True)
        assert result['status'] == 'force_done'

    @pytest.mark.asyncio
    async def test_moving_rejected_even_with_force(self, ready_app: App):
        ready_app.cars[1].direction = Direction.UP
        r1 = await ready_app.control_door(1, 'open', force=False)
        assert r1['status'] == 'rejected'
        assert '移动' in r1['message']
        r2 = await ready_app.control_door(1, 'open', force=True)
        assert r2['status'] == 'rejected'
        assert '移动' in r2['message']

    @pytest.mark.asyncio
    async def test_door_already_open_rejected(self, ready_app: App):
        car_lock_i = ready_app.mapper.addr_input('car_door_lock', 1)
        ready_app.io.observe_input(car_lock_i, 0)
        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'rejected'
        assert '门已开' in result['message']

    @pytest.mark.asyncio
    async def test_door_already_closed_rejected(self, ready_app: App):
        result = await ready_app.control_door(1, 'close', force=False)
        assert result['status'] == 'rejected'
        assert '门已关好' in result['message']


# ===== force 模式 =====

class TestForceMode:
    @pytest.mark.asyncio
    async def test_force_open_pulls_relay_immediately(self, ready_app: App):
        result = await ready_app.control_door(1, 'open', force=True)
        assert result['status'] == 'force_done'
        open_addr = ready_app.mapper.addr_output('door_open_relay', 1)
        assert ready_app.io_write[1].get_output(open_addr) == 1
        close_addr = ready_app.mapper.addr_output('door_close_relay', 1)
        assert ready_app.io_write[1].get_output(close_addr) == 0

    @pytest.mark.asyncio
    async def test_force_close_pulls_relay_immediately(self, ready_app: App):
        result = await ready_app.control_door(1, 'close', force=True)
        assert result['status'] == 'force_done'
        open_addr = ready_app.mapper.addr_output('door_open_relay', 1)
        close_addr = ready_app.mapper.addr_output('door_close_relay', 1)
        assert ready_app.io_write[1].get_output(open_addr) == 0
        assert ready_app.io_write[1].get_output(close_addr) == 1

    @pytest.mark.asyncio
    async def test_force_does_not_set_busy_flag(self, ready_app: App):
        """force 模式不应持有 _door_busy(立即完成)"""
        result = await ready_app.control_door(1, 'open', force=True)
        assert result['status'] == 'force_done'
        assert ready_app._door_busy[1] is False


# ===== 非阻塞 + 后台跟踪 =====

class TestNonBlocking:
    @pytest.mark.asyncio
    async def test_open_returns_dispatched_immediately(self, ready_app: App):
        """/door open 立即返回 dispatched,不卡"""
        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'
        # _door_busy[1] 应被设置(后台跟踪中)
        assert ready_app._door_busy[1] is True
        # 等待后台完成
        await _wait_for_busy_release(ready_app, 1)
        # 触发完成信号让后台结束(否则要等真实 PLC)
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)
        ready_app.io.simulate_input(door_open_done_i, 1)
        # 再等一下
        await _wait_for_busy_release(ready_app, 1, timeout=0.2)

    @pytest.mark.asyncio
    async def test_door_does_not_block_event_loop(self, ready_app: App):
        """control_door 立即返回,event loop 可处理其他 task"""
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        # 启动 control_door(后台跟踪)
        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'

        # 期间能执行其他 await
        await asyncio.sleep(0.01)

        # 现在触发完成 → 后台 task 释放 mutex
        ready_app.io.simulate_input(door_open_done_i, 1)
        # 等后台完成
        assert await _wait_for_busy_release(ready_app, 1, timeout=0.5)


# ===== 错层检测（后台 task 输出） =====

class TestWrongFloorDetection:
    @pytest.mark.asyncio
    async def test_open_wrong_floor_prints_warning(self, ready_app: App, capsys):
        """开门过程中:错误楼层 L3 门锁 false → 后台 task 打印 ⚠️"""
        floor_lock_3_i = ready_app.mapper.addr_input('floor_door_lock_3', 1)
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'

        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(floor_lock_3_i, 0)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(door_open_done_i, 1)

        # 等后台完成
        await _wait_for_busy_release(ready_app, 1)
        await asyncio.sleep(0.05)  # 让 print 刷新

        out = capsys.readouterr().out
        assert '开错楼' in out
        assert 'L3' in out

    @pytest.mark.asyncio
    async def test_open_correct_floor_no_warning(self, ready_app: App, capsys):
        """正常开门:仅对应楼层 L1 门锁 false → 后台 task 不打印 ⚠️"""
        floor_lock_1_i = ready_app.mapper.addr_input('floor_door_lock_1', 1)
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'

        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(floor_lock_1_i, 0)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(door_open_done_i, 1)

        await _wait_for_busy_release(ready_app, 1)
        await asyncio.sleep(0.05)

        out = capsys.readouterr().out
        assert '开错楼' not in out


# ===== /debug show door_status 监视 =====

class TestDoorStatusMonitor:
    @pytest.mark.asyncio
    async def test_default_off_no_print_on_completion(self, ready_app: App, capsys):
        """默认不开启 door_status → 完成时不打印 [door]"""
        c = Console(ready_app)
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        await ready_app.control_door(1, 'open', force=False)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)
        await asyncio.sleep(0.05)

        out = capsys.readouterr().out
        assert '[door]' not in out

    @pytest.mark.asyncio
    async def test_door_status_on_prints_completion(self, ready_app: App, capsys):
        """开启 door_status → 完成时打印 [door] car N 开门到位"""
        c = Console(ready_app)
        c._toggle_door_status_monitor()  # 开启
        capsys.readouterr()  # 清空 enable 的输出

        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        await ready_app.control_door(1, 'open', force=False)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)
        await asyncio.sleep(0.05)

        out = capsys.readouterr().out
        assert '[door] car 1 开门到位' in out

    @pytest.mark.asyncio
    async def test_door_status_off_after_on_no_print(self, ready_app: App, capsys):
        """toggle off 后完成时不打印"""
        c = Console(ready_app)
        c._toggle_door_status_monitor()  # on
        c._toggle_door_status_monitor()  # off
        capsys.readouterr()  # 清空 toggle 的输出

        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        await ready_app.control_door(1, 'open', force=False)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)
        await asyncio.sleep(0.05)

        out = capsys.readouterr().out
        assert '[door]' not in out

    @pytest.mark.asyncio
    async def test_wrong_floor_prints_even_without_monitor(self, ready_app: App, capsys):
        """错层错误:不依赖 monitor,始终打印"""
        c = Console(ready_app)
        # 不开启 monitor
        capsys.readouterr()  # 清空

        floor_lock_3_i = ready_app.mapper.addr_input('floor_door_lock_3', 1)
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)

        await ready_app.control_door(1, 'open', force=False)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(floor_lock_3_i, 0)
        await asyncio.sleep(0.02)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)
        await asyncio.sleep(0.05)

        out = capsys.readouterr().out
        # 错层始终打印,不依赖 monitor
        assert '开错楼' in out
        assert '[door]' not in out  # 但成功完成不打印


# ===== 同轿厢互斥 =====

class TestMutex:
    @pytest.mark.asyncio
    async def test_concurrent_same_car_rejected(self, ready_app: App):
        """同轿厢并发 /door open → 第二个 busy 拒绝"""
        task1 = asyncio.create_task(ready_app.control_door(1, 'open', force=False))
        await asyncio.sleep(0.01)
        result2 = await ready_app.control_door(1, 'open', force=False)
        assert result2['status'] == 'busy'
        # 让 task1 完成
        result1 = await task1
        assert result1['status'] == 'dispatched'
        # 等后台释放
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)
        await task1 if not task1.done() else None

    @pytest.mark.asyncio
    async def test_different_cars_no_mutex(self, ready_app: App):
        """不同轿厢 /door 可并发"""
        ready_app.cars[2].state = CarState.READY
        ready_app.cars[2].position = 1
        ready_app.cars[2].direction = Direction.IDLE
        car_lock_i = ready_app.mapper.addr_input('car_door_lock', 2)
        ready_app.io.observe_input(car_lock_i, 1)

        r1, r2 = await asyncio.gather(
            ready_app.control_door(1, 'open', force=False),
            ready_app.control_door(2, 'open', force=False),
        )
        assert r1['status'] == 'dispatched'
        assert r2['status'] == 'dispatched'
        # 清理
        door_open_done_i_1 = ready_app.mapper.addr_input('door_open_done', 1)
        door_open_done_i_2 = ready_app.mapper.addr_input('door_open_done', 2)
        ready_app.io.simulate_input(door_open_done_i_1, 1)
        ready_app.io.simulate_input(door_open_done_i_2, 1)
        await _wait_for_busy_release(ready_app, 1)
        await _wait_for_busy_release(ready_app, 2)


# ===== Console.cmd_door 命令层测试 =====

class TestConsoleCmdDoor:
    @pytest.mark.asyncio
    async def test_cmd_door_parses_force(self, ready_app: App, capsys):
        c = Console(ready_app)
        await c.cmd_door(['1', 'close', 'force'])
        out = capsys.readouterr().out
        assert 'force' in out.lower()
        assert '已拉' in out

    @pytest.mark.asyncio
    async def test_cmd_door_invalid_action(self, ready_app: App, capsys):
        c = Console(ready_app)
        await c.cmd_door(['1', 'bounce'])
        out = capsys.readouterr().out
        assert '参数错误' in out

    @pytest.mark.asyncio
    async def test_cmd_door_batch_all(self, ready_app: App, capsys):
        """所有轿厢 force close"""
        c = Console(ready_app)
        await c.cmd_door(['all', 'close', 'force'])
        out = capsys.readouterr().out
        lines = [line for line in out.split('\n') if 'car' in line]
        assert len(lines) == 6

    @pytest.mark.asyncio
    async def test_cmd_door_open_prints_dispatched(self, ready_app: App, capsys):
        """/door open 输出"已派发"消息"""
        c = Console(ready_app)
        await c.cmd_door(['1', 'open'])
        out = capsys.readouterr().out
        assert '已派发' in out
        assert '后台跟踪' in out
        # 清理
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)


# ===== 集成测试 =====

class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_cycle_init_open_close(self, app: App, capsys):
        """完整周期:init → open → close 各自正确返回"""
        car_id = 1
        # 1. 未 init → 拒绝
        r1 = await app.control_door(car_id, 'open', force=False)
        assert r1['status'] == 'rejected'

        # 2. 设 READY
        app.cars[car_id].state = CarState.READY
        app.cars[car_id].position = 1
        app.cars[car_id].direction = Direction.IDLE
        car_lock_i = app.mapper.addr_input('car_door_lock', car_id)
        floor_lock_1_i = app.mapper.addr_input('floor_door_lock_1', car_id)
        app.io.observe_input(car_lock_i, 1)
        app.io.observe_input(floor_lock_1_i, 1)

        # 3. /door open → dispatched
        r2 = await app.control_door(car_id, 'open', force=False)
        assert r2['status'] == 'dispatched'
        # 触发完成
        door_open_done_i = app.mapper.addr_input('door_open_done', car_id)
        app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(app, car_id)
        # 模拟门开了
        app.io.observe_input(car_lock_i, 0)

        # 4. 门开了,再 /door open 拒绝
        r3 = await app.control_door(car_id, 'open', force=False)
        assert r3['status'] == 'rejected'
        assert '门已开' in r3['message']

        # 5. /door close → dispatched
        r4 = await app.control_door(car_id, 'close', force=False)
        assert r4['status'] == 'dispatched'
        door_close_done_i = app.mapper.addr_input('door_close_done', car_id)
        app.io.simulate_input(door_close_done_i, 1)
        await _wait_for_busy_release(app, car_id)
        app.io.observe_input(car_lock_i, 1)

        # 6. 门关了,再 /door close 拒绝
        r5 = await app.control_door(car_id, 'close', force=False)
        assert r5['status'] == 'rejected'
        assert '门已关好' in r5['message']


class TestCompletionSignalRefactor:
    """验证完成信号改用 door_open_done / door_close_done"""

    @pytest.mark.asyncio
    async def test_car_door_lock_change_does_not_complete(self, ready_app: App):
        """car_door_lock 变化不再触发后台完成(_door_busy 仍为 True)"""
        car_lock_i = ready_app.mapper.addr_input('car_door_lock', 1)

        # control_door 立即返回 dispatched
        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'
        # 后台 task 在跟踪中,_door_busy[1] = True
        assert ready_app._door_busy[1] is True

        # 触发 car_door_lock=false(开了一点) → 后台不应完成
        ready_app.io.simulate_input(car_lock_i, 0)
        await asyncio.sleep(0.2)
        # 后台 task 还在等(因为 car_lock 不再触发 done_event)
        assert ready_app._door_busy[1] is True, \
            "car_door_lock 变化不应触发完成,只 door_open_done 才能"
        # 清理:触发 door_open_done 完成
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)


# ===== cron 兜底超时 =====

class TestCronTimeoutFallback:
    """door_complete_timeout 秒后未收到 done 信号,cron 兜底释放 _door_busy"""

    @pytest.mark.asyncio
    async def test_no_done_signal_releases_busy_via_cron(
        self, ready_app: App, capsys, monkeypatch
    ):
        """不发 door_open_done → cron 兜底在 door_complete_timeout 后释放 busy

        把 config 里的 door_complete_timeout 调成 0.2s(默认 8s)以加快测试。
        """
        # 缩短 timeout 加速测试
        ready_app.config.setdefault('elevator', {})['door_complete_timeout'] = 0.2
        timeout = ready_app.config['elevator']['door_complete_timeout']

        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'
        assert ready_app._door_busy[1] is True

        # 等 cron fire(timeout + 调度抖动)
        # 没 sleep / wait_for,直接等待后台 cron 事件循环
        deadline = asyncio.get_event_loop().time() + timeout + 0.5
        while asyncio.get_event_loop().time() < deadline:
            if not ready_app._door_busy[1]:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail('cron 兜底未在 timeout 内释放 _door_busy')

        # cron 已 fire:busy 释放 + 打印警告
        assert ready_app._door_busy[1] is False
        out = capsys.readouterr().out
        assert '门动作超时' in out
        assert 'door_open_done' in out

    @pytest.mark.asyncio
    async def test_done_signal_before_timeout_cancels_cron(
        self, ready_app: App
    ):
        """done 信号先到 → cron job 被取消,不会误触发"""
        # 短 timeout 让 cron 容易误触
        ready_app.config.setdefault('elevator', {})['door_complete_timeout'] = 0.5

        result = await ready_app.control_door(1, 'open', force=False)
        assert result['status'] == 'dispatched'
        job_name = 'door_timeout_1_open'
        # cron job 已 schedule
        assert job_name in ready_app.cron._jobs

        # 立即触发 done 信号(远在 timeout 之前)
        door_open_done_i = ready_app.mapper.addr_input('door_open_done', 1)
        ready_app.io.simulate_input(door_open_done_i, 1)
        await _wait_for_busy_release(ready_app, 1)

        # 后台 task 取消 cron job
        # 等 0.6s(timeout+0.1)确认 cron 不会误触
        await asyncio.sleep(0.6)
        assert job_name not in ready_app.cron._jobs, \
            "成功路径未取消 cron job,会误触发 timeout"
        assert ready_app._door_busy[1] is False


# ===== set_hall_indicator 方向校验 =====

class TestSetHallIndicatorValidation:
    @pytest.mark.asyncio
    async def test_invalid_direction_raises(self, ready_app: App):
        """set_hall_indicator 收到非 up/down 方向 → ValueError"""
        with pytest.raises(ValueError, match='direction 必须是'):
            await ready_app.set_hall_indicator(5, 'sideways', True)
        # 状态不应被污染
        assert ready_app.hall_indicator_state(5, 'up') is False
        assert ready_app.hall_indicator_state(5, 'down') is False

    @pytest.mark.asyncio
    async def test_valid_directions_pass(self, ready_app: App):
        """'up' / 'down' 都正常通过"""
        await ready_app.set_hall_indicator(3, 'up', True)
        assert ready_app.hall_indicator_state(3, 'up') is True
        await ready_app.set_hall_indicator(7, 'down', True)
        assert ready_app.hall_indicator_state(7, 'down') is True