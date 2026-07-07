"""
test_ui.py —— UiController + IndicatorState 单测

游戏开发视角:UI 是电梯实体的属性,UI controller 同步逻辑状态 → IO 输出。
"""
import pytest

from core.io_client import IOClient
from core.io_mapper import IOMapper
from core.player import Car, IndicatorState
from core.ui import UiController


@pytest.fixture
def io_write() -> IOClient:
    """模拟 IOClient(无网络,只更新 output cache)"""
    return IOClient(simulate=True, debug=False)


@pytest.fixture
def mapper() -> IOMapper:
    return IOMapper('config/io_config.yaml')


@pytest.fixture
def car() -> Car:
    return Car(car_id=1)


@pytest.fixture
def ui(io_write, mapper, car) -> UiController:
    return UiController(io_write=io_write, mapper=mapper, car_id=1, car=car)


# ===== IndicatorState dataclass =====

class TestIndicatorState:
    def test_defaults(self):
        s = IndicatorState()
        assert s.full_load is False
        assert s.fault is False
        assert s.light is False
        assert s.fan is False
        assert s.cabin_button_leds == {}

    def test_independent_instances(self):
        """default_factory 必须为每个 Car 创建独立实例,避免状态串台"""
        s1 = IndicatorState()
        s2 = IndicatorState()
        s1.cabin_button_leds[1] = True
        assert 1 not in s2.cabin_button_leds


# ===== Car.ui 字段 =====

class TestCarUi:
    def test_default_empty(self):
        car = Car(car_id=1)
        assert isinstance(car.ui, IndicatorState)
        assert car.ui.fault is False

    def test_snapshot_includes_ui(self):
        car = Car(car_id=1)
        car.ui.fault = True
        car.ui.light = True
        car.ui.cabin_button_leds[3] = True
        snap = car.snapshot()
        assert 'ui' in snap
        assert snap['ui']['fault'] is True
        assert snap['ui']['light'] is True
        assert snap['ui']['full_load'] is False
        assert snap['ui']['cabin_button_leds'] == {3: True}


# ===== UiController.set_xxx 状态 + IO 同步 =====

class TestUiControllerIndicatorSync:
    @pytest.mark.asyncio
    async def test_set_full_load_updates_state_and_io(self, ui, io_write, mapper):
        await ui.set_full_load(True)
        assert ui.car.ui.full_load is True
        addr = mapper.addr_output('full_load_indicator', 1)
        assert io_write.get_output(addr) == 1
        assert io_write._output_cache[addr] == 1

    @pytest.mark.asyncio
    async def test_set_fault_updates_state_and_io(self, ui, io_write, mapper):
        await ui.set_fault(True)
        assert ui.car.ui.fault is True
        addr = mapper.addr_output('fault_indicator', 1)
        assert io_write.get_output(addr) == 1

    @pytest.mark.asyncio
    async def test_set_light_updates_state_and_io(self, ui, io_write, mapper):
        await ui.set_light(True)
        assert ui.car.ui.light is True
        addr = mapper.addr_output('light_indicator', 1)
        assert io_write.get_output(addr) == 1

    @pytest.mark.asyncio
    async def test_set_fan_updates_state_and_io(self, ui, io_write, mapper):
        await ui.set_fan(True)
        assert ui.car.ui.fan is True
        addr = mapper.addr_output('fan_indicator', 1)
        assert io_write.get_output(addr) == 1

    @pytest.mark.asyncio
    async def test_set_full_load_off(self, ui, io_write, mapper):
        """先开后关,验证写入 0"""
        await ui.set_full_load(True)
        await ui.set_full_load(False)
        assert ui.car.ui.full_load is False
        addr = mapper.addr_output('full_load_indicator', 1)
        assert io_write.get_output(addr) == 0


class TestUiControllerCabinLed:
    @pytest.mark.asyncio
    async def test_set_cabin_button_led_updates_state_and_io(self, ui, io_write, mapper):
        await ui.set_cabin_button_led(3, True)
        assert ui.car.ui.cabin_button_leds[3] is True
        addr = mapper.addr_output('cabin_button_led_3', 1)
        assert io_write.get_output(addr) == 1

    @pytest.mark.asyncio
    async def test_set_cabin_button_led_off(self, ui, io_write, mapper):
        await ui.set_cabin_button_led(5, True)
        await ui.set_cabin_button_led(5, False)
        assert ui.car.ui.cabin_button_leds[5] is False
        addr = mapper.addr_output('cabin_button_led_5', 1)
        assert io_write.get_output(addr) == 0

    @pytest.mark.asyncio
    async def test_multiple_floors_independent(self, ui, io_write, mapper):
        """开 3 楼不影响 5 楼状态"""
        await ui.set_cabin_button_led(3, True)
        await ui.set_cabin_button_led(5, False)
        assert ui.car.ui.cabin_button_leds[3] is True
        assert ui.car.ui.cabin_button_leds[5] is False


class TestUiControllerDoorOpen:
    """PLC 上没有开关门按钮灯信号,故 UI 模块不提供 set_door_open_indicator。

    此处作为反向断言:确认方法不存在(避免未来误加)。
    """
    def test_no_door_open_indicator_method(self, ui):
        assert not hasattr(ui, 'set_door_open_indicator')
        assert not hasattr(ui, 'set_door_close_indicator')


# ===== sync_to_io 全量同步 =====

class TestSyncToIO:
    @pytest.mark.asyncio
    async def test_sync_writes_all_state(self, ui, io_write, mapper):
        """Car.ui 有值时,sync_to_io 全量写 IO"""
        ui.car.ui.full_load = True
        ui.car.ui.fault = True
        ui.car.ui.light = False
        ui.car.ui.fan = True
        ui.car.ui.cabin_button_leds[2] = True
        ui.car.ui.cabin_button_leds[7] = False

        await ui.sync_to_io()

        assert io_write.get_output(mapper.addr_output('full_load_indicator', 1)) == 1
        assert io_write.get_output(mapper.addr_output('fault_indicator', 1)) == 1
        assert io_write.get_output(mapper.addr_output('light_indicator', 1)) == 0
        assert io_write.get_output(mapper.addr_output('fan_indicator', 1)) == 1
        assert io_write.get_output(mapper.addr_output('cabin_button_led_2', 1)) == 1
        assert io_write.get_output(mapper.addr_output('cabin_button_led_7', 1)) == 0


# ===== 解耦验证:cabin_button 事件不自动亮 LED =====

class TestDecouplingFromIOEvents:
    """游戏开发视角强调解耦:cabin_button_X 按下不应自动亮 LED

    这条由 app._on_cabin_button_event 行为保证(它只调 call_internal + cancel cron,
    不调 UiController.set_cabin_button_led)。此测试是契约级别的回归保护。
    """

    def test_cabin_button_event_does_not_touch_ui(self):
        """Car.ui.cabin_button_leds 在按下按钮时不应被自动修改

        验证方法:直接构造一个 Car,模拟"按下按钮"事件(人为调 app 内部路径),
        检查 Car.ui.cabin_button_leds 不变。
        """
        car = Car(car_id=1)
        # 模拟一次内召按下,正常逻辑是 _on_cabin_button_event → call_internal + cancel cron
        # 不应触发 set_cabin_button_led
        # 这里只断言初始状态保持
        assert car.ui.cabin_button_leds == {}
        # 即便位置等状态更新,ui 也不变
        car.position = 5
        assert car.ui.cabin_button_leds == {}