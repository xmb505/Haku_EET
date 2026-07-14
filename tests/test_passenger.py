"""
test_passenger.py —— 乘客交互模块（大脑）单元测试
"""
import asyncio
from pathlib import Path

import pytest

from core.actions import Action, ActionKind
from core.app import App
from core.cron import CronJob
from core.passenger import PassengerManager, PassengerQueue
from core.player import CarState, Direction, DoorState
from core.io_client import IOEvent

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
    """构建一个 IO 事件"""
    return IOEvent(i_addr=mapper.addr_input(signal, car_id), bit=bit)


def hall_call_i_addr(app, signal: str) -> str:
    """hall_call 信号是 car_id=0 的全局信号，转成 I 地址"""
    return app.mapper.addr_input(signal, 0)


# ======================================================================
# PassengerQueue 单元测试
# ======================================================================


class TestPassengerQueueDiscard:
    """DISCARD 模式：顺向接受，已过站丢弃"""

    def test_up_valid_sorted(self):
        """上行：cache {8, 3, 5} pos=2 target=9 → [3, 5, 8]"""
        q = PassengerQueue(mode='discard')
        q.compile({8, 3, 5}, car_position=2,
                   car_direction=Direction.UP, current_target=9)
        assert q.items == [3, 5, 8]

    def test_up_filter_past(self):
        """上行：pos=7 target=10 cache {4, 8, 6} → [8]（4 和 6 已过站）"""
        q = PassengerQueue(mode='discard')
        q.compile({4, 8, 6}, car_position=7,
                   car_direction=Direction.UP, current_target=10)
        assert q.items == [8]

    def test_up_filter_beyond_target(self):
        """上行：pos=5 target=9 cache {12} → []（12 超过目标范围）"""
        q = PassengerQueue(mode='discard')
        q.compile({12}, car_position=5,
                   car_direction=Direction.UP, current_target=9)
        assert q.items == []

    def test_down_valid_sorted(self):
        """下行：cache {8, 3, 5} pos=10 target=2 → [8, 5, 3]"""
        q = PassengerQueue(mode='discard')
        q.compile({8, 3, 5}, car_position=10,
                   car_direction=Direction.DOWN, current_target=2)
        assert q.items == [8, 5, 3]

    def test_down_filter_past(self):
        """下行：pos=4 target=2 cache {8, 3} → [3]（8 已过站）"""
        q = PassengerQueue(mode='discard')
        q.compile({8, 3}, car_position=4,
                   car_direction=Direction.DOWN, current_target=2)
        assert q.items == [3]

    def test_idle_sorted(self):
        """IDLE：cache {8, 3, 5} → [3, 5, 8]"""
        q = PassengerQueue(mode='discard')
        q.compile({8, 3, 5}, car_position=5,
                   car_direction=Direction.IDLE, current_target=None)
        assert q.items == [3, 5, 8]

    def test_empty_cache(self):
        """空缓存 → []"""
        q = PassengerQueue(mode='discard')
        q.compile(set(), car_position=5,
                   car_direction=Direction.IDLE, current_target=None)
        assert q.items == []

    def test_mark_served(self):
        """标记完成：已有 [3, 5, 8] mark_served(5) → [3, 8]"""
        q = PassengerQueue(mode='discard')
        q.compile({3, 5, 8}, car_position=2,
                   car_direction=Direction.UP, current_target=9)
        q.mark_served(5)
        assert q.items == [3, 8]

    def test_next_target(self):
        """next_target 返回第一个不移除"""
        q = PassengerQueue(mode='discard')
        q.compile({3, 5}, car_position=2,
                   car_direction=Direction.UP, current_target=9)
        assert q.next_target() == 3
        assert q.items == [3, 5]  # 不移除


class TestPassengerQueueKeep:
    """KEEP 模式：全部保留，先顺向后反向"""

    def test_up_after_before(self):
        """UP pos=5 target=10 cache {3, 8} → [8, 3]（先顺向 8，再回头 3）"""
        q = PassengerQueue(mode='keep')
        q.compile({3, 8}, car_position=5,
                   car_direction=Direction.UP, current_target=10)
        assert q.items == [8, 3]

    def test_down_before_after(self):
        """DOWN pos=6 target=2 cache {8, 3} → [3, 8]（先顺向 3，再回头 8）"""
        q = PassengerQueue(mode='keep')
        q.compile({8, 3}, car_position=6,
                   car_direction=Direction.DOWN, current_target=2)
        assert q.items == [3, 8]

    def test_idle_all(self):
        """IDLE pos=5 cache {3, 8, 1} → [1, 3, 8]（全部升序）"""
        q = PassengerQueue(mode='keep')
        q.compile({3, 8, 1}, car_position=5,
                   car_direction=Direction.IDLE, current_target=None)
        assert q.items == [1, 3, 8]


    def test_keep_empty_cache(self):
        """空缓存 → []"""
        q = PassengerQueue(mode='keep')
        q.compile(set(), car_position=5,
                   car_direction=Direction.IDLE, current_target=None)
        assert q.items == []


# ======================================================================
# PassengerManager 集成测试
# ======================================================================


def _init_all_cars(app, pos=1):
    """初始化所有轿厢（pass set_usermode 检查）"""
    for cid in app.car_ids:
        app.cars[cid].state = CarState.READY
        app.cars[cid].position = pos


class TestPassengerManagerHallCall:
    """外召事件测试"""

    @pytest.mark.asyncio
    async def test_hall_call_same_floor_opens_door(self, app: App):
        """外召同层：hall_call_up_1 + car at L1 → 开门"""
        # 准备：所有车就绪，启用 usermode
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1

        # 模拟 hall_call_up_1 按下
        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.1)

        # 验证：OPEN_DOOR 已由 executor 开始处理
        assert app.cars[1].door_state in (
            DoorState.OPENING, DoorState.OPEN), f'door_state 应为 OPENING/OPEN，实际 {app.cars[1].door_state}'

    @pytest.mark.asyncio
    async def test_hall_call_pickup_state(self, app: App):
        """外召后被标记为接客中"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1

        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        pm = app.pm
        assert pm._pickup_active[1].get((1, 'up'), False) is True

    @pytest.mark.asyncio
    async def test_hall_call_no_car_ready(self, app: App):
        """无 Ready 车 → 不处理"""
        # 直接启用 usermode（不管 set_usermode 的预检）
        app._usermode = True
        # 所有车 UNKNOWN

        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        pm = app.pm
        # 没有车被标记为接客
        for cid in app.car_ids:
            assert not any(pm._pickup_active[cid].values())

    @pytest.mark.asyncio
    async def test_hall_call_usermode_off_ignored(self, app: App):
        """usermode 关闭时外召被忽略"""
        _init_all_cars(app)
        await app.set_usermode(False)
        app.cars[1].position = 1

        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        assert app.cars[1].door_state == DoorState.CLOSED
        assert app.action_queues[1].empty()

    @pytest.mark.asyncio
    async def test_hall_call_dispatch_remote(self, app: App):
        """外召不同层 → 发起 MOVE（非直接开门）"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1
        app.cars[2].position = 8  # car 2 更近（dist 1 < car1 dist 5）

        # hall_call_up_7 → 派给 car2（位置 8 离 7 最近）
        i_addr = hall_call_i_addr(app, 'hall_call_up_7')
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        # car2 应有 pending 任务
        assert 7 in app.pending_calls[2], f'car2 pending 应有 7，实际 {app.pending_calls[2]}'

    @pytest.mark.asyncio
    async def test_hall_call_falling_starts_close_cron(self, app: App):
        """外召松开 → 启动关门 cron"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1

        # 按下 → 开门
        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        # 设门为 OPEN（模拟 executor 开门完成）
        app.cars[1].door_state = DoorState.OPEN
        pm = app.pm
        await pm.on_action_done(1, Action(ActionKind.OPEN_DOOR))

        # 松开 → 启动 cron
        app.io.simulate_input(i_addr, 0)
        await asyncio.sleep(0.05)

        jn = pm._close_door_job_name(1)
        assert jn in pm._app.cron._jobs, f'关门 cron {jn} 应存在'


class TestPassengerManagerCabinButton:
    """内召事件测试"""

    @pytest.mark.asyncio
    async def test_cabin_button_door_open_caches(self, app: App):
        """门开着时按内召 → 缓存"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 5
        app.cars[1].door_state = DoorState.OPEN

        pm = app.pm

        # 模拟 cabin_button_7
        i_addr = app.mapper.addr_input('cabin_button_7', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        assert 7 in pm._button_cache[1], f'cache 应有 7，实际 {pm._button_cache[1]}'
        assert app.cars[1].human_presence == 1

    @pytest.mark.asyncio
    async def test_cabin_button_door_closed_calls(self, app: App):
        """门关着时按内召 → 入队 + 发起 call_internal"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 2
        app.cars[1].door_state = DoorState.CLOSED

        pm = app.pm

        # 模拟 cabin_button_7
        i_addr = app.mapper.addr_input('cabin_button_7', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.1)

        # pending_calls 应有 7
        assert 7 in app.pending_calls[1], f'pending 应有 7，实际 {app.pending_calls[1]}'

    @pytest.mark.asyncio
    async def test_cabin_button_led_lit(self, app: App):
        """按内召 → 轿内按钮 LED 点亮"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 5
        app.cars[1].door_state = DoorState.OPEN

        pm = app.pm

        i_addr = app.mapper.addr_input('cabin_button_7', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        assert app.cars[1].ui.cabin_button_leds.get(7) is True


class TestPassengerManagerDoorFlow:
    """开关门流程测试"""

    @pytest.mark.asyncio
    async def test_door_opened_clears_indicator(self, app: App):
        """开门完成 → 灭外召灯 + 清接客状态"""
        app.cars[1].state = CarState.READY
        app.cars[1].position = 3

        pm = app.pm
        pm._pickup_active[1][(3, 'up')] = True
        await app.set_hall_indicator(3, 'up', True)

        await pm.on_action_done(1, Action(ActionKind.OPEN_DOOR))

        # 外召灯应灭
        assert app.hall_indicator_state(3, 'up') is False
        # 按钮缓存重置
        assert pm._button_cache[1] == set()

    @pytest.mark.asyncio
    async def test_door_closed_compiles_queue(self, app: App):
        """关门完成 → 编译乘客缓存 → 发送 MOVE"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 2
        app.cars[1].direction = Direction.UP

        pm = app.pm
        pm._button_cache[1] = {5, 8}  # 开门期间收集的请求

        await pm.on_action_done(1, Action(ActionKind.CLOSE_DOOR))
        await asyncio.sleep(0.05)

        # 队列应编译为 [5, 8]
        assert pm._passenger_queue[1].items == [5, 8]
        # 缓存应清空
        assert pm._button_cache[1] == set()
        # pending_calls 应有 5
        assert 5 in app.pending_calls[1], f'pending 应有 5，实际 {app.pending_calls[1]}'

    @pytest.mark.asyncio
    async def test_door_closed_empty_queue_starts_lights_off(self, app: App):
        """关门完成 + 无请求 → 启动熄灯 cron"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 2

        pm = app.pm
        pm._button_cache[1] = set()

        await pm.on_action_done(1, Action(ActionKind.CLOSE_DOOR))
        await asyncio.sleep(0.05)

        jn = pm._lights_off_job_name(1)
        assert jn in pm._app.cron._jobs, f'熄灯 cron {jn} 应存在'


class TestPassengerManagerDismissedFloors:
    """已过站楼层 DISCARD 行为测试"""

    @pytest.mark.asyncio
    async def test_discard_led_stays_on(self, app: App):
        """DISCARD 模式下，丢弃的楼层 LED 应保持亮到终点站"""
        await app.set_usermode(True)
        app.cars[1].state = CarState.READY
        app.cars[1].position = 5
        app.cars[1].direction = Direction.UP
        app.cars[1].target_floor = 10

        pm = app.pm
        pm._enabled = True
        pm._button_cache[1] = {3, 8}  # 3 已过站，8 顺向

        await app.ui[1].set_cabin_button_led(3, True)
        await app.ui[1].set_cabin_button_led(8, True)

        # 关门 → 编译（DISCARD 模式）
        await pm.on_action_done(1, Action(ActionKind.CLOSE_DOOR))

        # 队列应只有 8
        assert pm._passenger_queue[1].items == [8]
        # 3 的 LED 应保持亮（到终点站才灭）
        assert app.cars[1].ui.cabin_button_leds.get(3) is True
        # 8 的 LED 应保持亮（正常）
        assert app.cars[1].ui.cabin_button_leds.get(8) is True


class TestPassengerManagerEmergencyReset:
    """紧急停止和重置测试"""

    @pytest.mark.asyncio
    async def test_emergency_clears_passenger_state(self, app: App):
        """紧急停止 → 清空所有大脑状态"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 5

        pm = app.pm
        pm._button_cache[1] = {3, 7}
        pm._passenger_queue[1].compile({4}, 5, Direction.UP, 8)
        pm._pickup_active[1][(3, 'up')] = True

        await pm.on_emergency(1)

        assert pm._button_cache[1] == set()
        assert pm._passenger_queue[1].items == []
        assert not any(pm._pickup_active[1].values())

    @pytest.mark.asyncio
    async def test_reset_clears_passenger_state(self, app: App):
        """reset → 清空所有大脑状态"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 5

        pm = app.pm
        pm._button_cache[1] = {3, 7}
        pm._passenger_queue[1].compile({4}, 5, Direction.UP, 8)
        pm._pickup_active[1][(3, 'up')] = True

        await pm.reset(1)

        assert pm._button_cache[1] == set()
        assert pm._passenger_queue[1].items == []
        assert not any(pm._pickup_active[1].values())


class TestPassengerManagerDoorButtons:
    """门按钮事件测试"""

    @pytest.mark.asyncio
    async def test_door_open_button_opens(self, app: App):
        """门关着时按开门按钮 → 开门"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 3
        app.cars[1].door_state = DoorState.CLOSED

        # 模拟 door_open_button
        i_addr = app.mapper.addr_input('door_open_button', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.1)

        assert app.cars[1].door_state != DoorState.CLOSED

    @pytest.mark.asyncio
    async def test_door_close_button_closes(self, app: App):
        """门开着时按关门按钮 → 立即关门"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 3
        app.cars[1].door_state = DoorState.OPEN

        # 模拟 door_close_button
        i_addr = app.mapper.addr_input('door_close_button', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.1)

        assert app.cars[1].door_state == DoorState.CLOSING

    @pytest.mark.asyncio
    async def test_door_open_button_cancels_close_cron(self, app: App):
        """关门倒计时中按开门按钮 → 取消 cron"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 3
        app.cars[1].door_state = DoorState.OPEN

        pm = app.pm

        # 先启动关门 cron
        jn = pm._close_door_job_name(1)
        await pm._start_close_door_cron(1, 3, 'up')
        assert jn in pm._app.cron._jobs

        # 按开门按钮
        i_addr = app.mapper.addr_input('door_open_button', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)

        # cron 应该被取消
        assert jn not in pm._app.cron._jobs


class TestDoorCloseHallButtonProtection:
    """外召按钮按住时关门保护测试"""

    @pytest.mark.asyncio
    async def test_door_close_button_ignored_when_hall_held(self, app: App):
        """外召按钮按住时按关门按钮 → 不响应关门"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1
        app.cars[1].door_state = DoorState.OPEN

        pm = app.pm
        # 设置外召 pickup 激活
        pm._pickup_active[1][(1, 'up')] = True

        # IO cache 中 hall_call_up_1 = 1（按钮按住）
        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)

        # 清空 action queue
        while not app.action_queues[1].empty():
            app.action_queues[1].get_nowait()

        # 按关门按钮
        await pm.on_door_button(1, 'door_close_button')

        # action_queue 中不应有 CLOSE_DOOR
        assert app.action_queues[1].empty(), '外召按住时关门按钮不应发送 CLOSE_DOOR'

    @pytest.mark.asyncio
    async def test_door_close_cron_aborted_when_hall_held(self, app: App):
        """关门 cron 触发时外召仍按住 → 不执行关门"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1
        app.cars[1].door_state = DoorState.OPEN

        pm = app.pm
        # 设置外召 pickup 激活
        pm._pickup_active[1][(1, 'up')] = True

        # IO cache 中 hall_call_up_1 = 1（按钮按住）
        i_addr = hall_call_i_addr(app, 'hall_call_up_1')
        app.io.simulate_input(i_addr, 1)

        # 清空 action queue
        while not app.action_queues[1].empty():
            app.action_queues[1].get_nowait()

        # 调度关门 cron（delay=0 以便立即触发）
        jn = pm._close_door_job_name(1)
        await pm._schedule_close_door_cron_job(1, jn, floor=1, direction='up')

        # 手动触发 cron 的 action
        job = app.cron._jobs.get(jn)
        assert job is not None, 'cron job 应存在'
        await job.action()

        # action_queue 中不应有 CLOSE_DOOR
        assert app.action_queues[1].empty(), '外召按住时 cron 不应发送 CLOSE_DOOR'


class TestDoorOpenButtonRelease:
    """开门按钮松开 → 启关门 cron"""

    @pytest.mark.asyncio
    async def test_door_open_button_release_schedules_close_cron(self, app: App):
        """门开着时松开开门按钮 → 启关门 cron"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 3
        app.cars[1].door_state = DoorState.OPEN

        pm = app.pm
        jn = pm._close_door_job_name(1)
        # 确保起始无 cron
        await pm._app.cron.cancel(jn)
        assert jn not in pm._app.cron._jobs

        # 模拟开门按钮松开 (bit=0)
        i_addr = app.mapper.addr_input('door_open_button', 1)
        app.io.simulate_input(i_addr, 0)
        await asyncio.sleep(0.1)

        # cron 应被启动
        assert jn in pm._app.cron._jobs, '开门松开后应启关门 cron'


class TestDoorCloseOpenButtonProtection:
    """开门按钮按住时关门保护测试"""

    @pytest.mark.asyncio
    async def test_door_close_button_ignored_when_open_held(self, app: App):
        """开门按钮按住时按关门按钮 → 不响应关门"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1
        app.cars[1].door_state = DoorState.OPEN

        # 模拟开门按钮按住
        i_addr = app.mapper.addr_input('door_open_button', 1)
        app.io.simulate_input(i_addr, 1)

        # 清空 action queue
        while not app.action_queues[1].empty():
            app.action_queues[1].get_nowait()

        # 按关门按钮
        await app.pm.on_door_button(1, 'door_close_button', bit=1)

        # action_queue 中不应有 CLOSE_DOOR
        assert app.action_queues[1].empty(), \
            '开门按钮按住时关门按钮不应发送 CLOSE_DOOR'

    @pytest.mark.asyncio
    async def test_door_close_button_works_when_open_released(self, app: App):
        """开门按钮松开后按关门按钮 → 正常关门"""
        _init_all_cars(app)
        await app.set_usermode(True)
        app.cars[1].position = 1
        app.cars[1].door_state = DoorState.OPEN

        # 模拟开门按钮按下后松开
        i_addr = app.mapper.addr_input('door_open_button', 1)
        app.io.simulate_input(i_addr, 1)
        await asyncio.sleep(0.05)
        app.io.simulate_input(i_addr, 0)
        await asyncio.sleep(0.05)

        # 清空 action queue
        while not app.action_queues[1].empty():
            app.action_queues[1].get_nowait()

        # 按关门按钮
        await app.pm.on_door_button(1, 'door_close_button', bit=1)

        # action_queue 中应有 CLOSE_DOOR
        actions = []
        while not app.action_queues[1].empty():
            actions.append(app.action_queues[1].get_nowait())
        assert any(a.kind == ActionKind.CLOSE_DOOR for a in actions), \
            '开门按钮松开后关门按钮应发送 CLOSE_DOOR'
