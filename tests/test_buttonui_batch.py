"""
test_buttonui_batch.py —— /buttonui in 多车多楼层批量支持

验证 _parse_floor_list + _buttonui_in 的批量笛卡尔积语义。
"""
from pathlib import Path

import pytest

from core.app import App
from core.console import Console

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'config.yaml'
IO_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'io_config.yaml'
DISPLAY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'display_config.yaml'


@pytest.fixture
async def app_and_console():
    """构造一个 simulate 模式 app + console,测试结束清理"""
    a = App(
        config_path=CONFIG_PATH,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    await a.start()
    c = Console(a)
    yield a, c
    await a.stop()


# ===== _parse_floor_list 单测 =====

class TestParseFloorList:
    @pytest.mark.asyncio
    async def test_single(self, app_and_console):
        a, c = app_and_console
        assert c._parse_floor_list('3') == [3]

    @pytest.mark.asyncio
    async def test_comma_list(self, app_and_console):
        a, c = app_and_console
        assert c._parse_floor_list('1,2,3') == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_range(self, app_and_console):
        a, c = app_and_console
        assert c._parse_floor_list('1-5') == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_range_full(self, app_and_console):
        a, c = app_and_console
        assert c._parse_floor_list('1-10') == list(range(1, 11))

    @pytest.mark.asyncio
    async def test_all_keyword(self, app_and_console):
        a, c = app_and_console
        assert c._parse_floor_list('all') == list(range(1, 11))

    @pytest.mark.asyncio
    async def test_invalid_floor_raises(self, app_and_console):
        a, c = app_and_console
        with pytest.raises(ValueError, match='楼层超出范围'):
            c._parse_floor_list('11')

    @pytest.mark.asyncio
    async def test_zero_floor_raises(self, app_and_console):
        a, c = app_and_console
        with pytest.raises(ValueError, match='楼层超出范围'):
            c._parse_floor_list('0')

    @pytest.mark.asyncio
    async def test_invalid_range_raises(self, app_and_console):
        a, c = app_and_console
        with pytest.raises(ValueError, match='楼层范围错误'):
            c._parse_floor_list('5-3')  # lo > hi

    @pytest.mark.asyncio
    async def test_range_out_of_bounds_raises(self, app_and_console):
        a, c = app_and_console
        with pytest.raises(ValueError, match='楼层范围错误'):
            c._parse_floor_list('0-10')  # lo < 1


# ===== _buttonui_in 批量语义 =====

class TestButtonuiInBatch:
    @pytest.mark.asyncio
    async def test_user_example_1_2_3_and_1_to_8_true(
        self, app_and_console, capsys
    ):
        """用户原始示例:1,2,3 号梯 × 1-8 楼全亮

        /buttonui in 1,2,3 1,2,3,4,5,6,7,8 true
        """
        a, c = app_and_console
        await c._buttonui_in(['1,2,3', '1,2,3,4,5,6,7,8', 'true'])
        # 3 车 × 8 层 = 24 个 LED 全亮
        for cid in [1, 2, 3]:
            for floor in range(1, 9):
                assert a.cars[cid].ui.cabin_button_leds[floor] is True
        # 9、10 楼应不受影响(不在用户给的列表里)
        for cid in [1, 2, 3]:
            assert 9 not in a.cars[cid].ui.cabin_button_leds
            assert 10 not in a.cars[cid].ui.cabin_button_leds
        out = capsys.readouterr().out
        # 批量输出:3 车各一行摘要(不再是 24 行逐 LED)
        assert 'car 1: 1-8 楼 LED 全亮 (8 个)' in out
        assert 'car 2: 1-8 楼 LED 全亮 (8 个)' in out
        assert 'car 3: 1-8 楼 LED 全亮 (8 个)' in out
        # 不再逐 LED 输出
        assert 'cabin_button_led_' not in out

    @pytest.mark.asyncio
    async def test_range_syntax(self, app_and_console):
        """/buttonui in 1 1-5 true → 车 1 的 1-5 楼全亮"""
        a, c = app_and_console
        await c._buttonui_in(['1', '1-5', 'true'])
        for floor in range(1, 6):
            assert a.cars[1].ui.cabin_button_leds[floor] is True
        # 6-10 楼不应受影响
        for floor in range(6, 11):
            assert floor not in a.cars[1].ui.cabin_button_leds

    @pytest.mark.asyncio
    async def test_all_keyword(self, app_and_console):
        """/buttonui in 1 all true → 车 1 所有 10 个 LED 全亮"""
        a, c = app_and_console
        await c._buttonui_in(['1', 'all', 'true'])
        for floor in range(1, 11):
            assert a.cars[1].ui.cabin_button_leds[floor] is True

    @pytest.mark.asyncio
    async def test_all_cars_comma_floors(self, app_and_console):
        """/buttonui in 1,2,3,4,5,6 3 true → 全部 6 部车 3 楼亮"""
        a, c = app_and_console
        await c._buttonui_in(['1,2,3,4,5,6', '3', 'true'])
        for cid in range(1, 7):
            assert a.cars[cid].ui.cabin_button_leds[3] is True

    @pytest.mark.asyncio
    async def test_off_state(self, app_and_console):
        """先开再关,验证 false 写 0"""
        a, c = app_and_console
        await c._buttonui_in(['1,2', '1,3,5', 'true'])
        await c._buttonui_in(['1,2', '1,3,5', 'false'])
        for cid in [1, 2]:
            for floor in [1, 3, 5]:
                assert a.cars[cid].ui.cabin_button_leds[floor] is False

    @pytest.mark.asyncio
    async def test_toggle_independently(self, app_and_console):
        """省略 true/false 时,每个 LED 独立 toggle 自身当前状态"""
        a, c = app_and_console
        # 先设 1 号梯 1 楼亮,2 楼灭
        await c._buttonui_in(['1', '1', 'true'])
        await c._buttonui_in(['1', '2', 'false'])
        assert a.cars[1].ui.cabin_button_leds[1] is True
        assert a.cars[1].ui.cabin_button_leds[2] is False

        # /buttonui in 1 1,2 (toggle)
        await c._buttonui_in(['1', '1,2'])
        # 1 楼翻转成 False,2 楼翻转成 True
        assert a.cars[1].ui.cabin_button_leds[1] is False
        assert a.cars[1].ui.cabin_button_leds[2] is True

    @pytest.mark.asyncio
    async def test_invalid_floor_errors(self, app_and_console, capsys):
        a, c = app_and_console
        await c._buttonui_in(['1', '11', 'true'])
        out = capsys.readouterr().out
        assert '楼层超出范围' in out
        # 没有任何 LED 被设
        assert a.cars[1].ui.cabin_button_leds == {}

    @pytest.mark.asyncio
    async def test_invalid_car_errors(self, app_and_console, capsys):
        a, c = app_and_console
        await c._buttonui_in(['99', '1', 'true'])
        out = capsys.readouterr().out
        assert '参数错误' in out
        # 没有任何 LED 被设
        assert a.cars[1].ui.cabin_button_leds == {}

    @pytest.mark.asyncio
    async def test_io_actually_written(self, app_and_console):
        """验证 IO 实际写到对应 DB 地址(不仅仅是 Car.ui 状态)"""
        a, c = app_and_console
        await c._buttonui_in(['1', '3,5', 'true'])
        addr3 = a.mapper.addr_output('cabin_button_led_3', 1)
        addr5 = a.mapper.addr_output('cabin_button_led_5', 1)
        assert a.io_write[1].get_output(addr3) == 1
        assert a.io_write[1].get_output(addr5) == 1

        await c._buttonui_in(['1', '3', 'false'])
        assert a.io_write[1].get_output(addr3) == 0
        assert a.io_write[1].get_output(addr5) == 1  # 5 楼不动


# ===== _buttonui_out 批量语义 =====

class TestButtonuiOutBatch:
    """验证 /buttonui out 也支持批量楼层,且按 direction 校验范围:

    up:   1-9(10 没上行按钮)
    down: 2-10(1 没下行按钮)

    不在范围内的楼层被跳过+警告,不整批 reject。
    """

    @pytest.mark.asyncio
    async def test_up_range_full(self, app_and_console):
        """/buttonui out 1-9 up true → 1-9 上行指示灯全亮"""
        a, c = app_and_console
        await c._buttonui_out(['1-9', 'up', 'true'])
        for floor in range(1, 10):
            assert a.hall_indicator_state(floor, 'up') is True

    @pytest.mark.asyncio
    async def test_down_range_full(self, app_and_console):
        """/buttonui out 2-10 down true → 2-10 下行指示灯全亮"""
        a, c = app_and_console
        await c._buttonui_out(['2-10', 'down', 'true'])
        for floor in range(2, 11):
            assert a.hall_indicator_state(floor, 'down') is True

    @pytest.mark.asyncio
    async def test_comma_list(self, app_and_console):
        """/buttonui out 1,3,5 up true"""
        a, c = app_and_console
        await c._buttonui_out(['1,3,5', 'up', 'true'])
        for floor in [1, 3, 5]:
            assert a.hall_indicator_state(floor, 'up') is True
        # 2、4 没被设
        assert a.hall_indicator_state(2, 'up') is False
        assert a.hall_indicator_state(4, 'up') is False

    @pytest.mark.asyncio
    async def test_all_keyword_for_up(self, app_and_console):
        """/buttonui out all up true → 自动跳过 10(没上行按钮),实际只设 1-9"""
        a, c = app_and_console
        await c._buttonui_out(['all', 'up', 'true'])
        for floor in range(1, 10):
            assert a.hall_indicator_state(floor, 'up') is True
        # 10 应跳过,不被设
        # (10 没 up 信号,即使被调 io.set 也会 KeyError,所以默认 False)

    @pytest.mark.asyncio
    async def test_all_keyword_for_down(self, app_and_console):
        """/buttonui out all down true → 自动跳过 1(没下行按钮),实际只设 2-10"""
        a, c = app_and_console
        await c._buttonui_out(['all', 'down', 'true'])
        for floor in range(2, 11):
            assert a.hall_indicator_state(floor, 'down') is True

    @pytest.mark.asyncio
    async def test_invalid_up_floor_skipped(self, app_and_console, capsys):
        """/buttonui out 10 up true → 10 没上行,应跳过 + 警告"""
        a, c = app_and_console
        await c._buttonui_out(['10', 'up', 'true'])
        out = capsys.readouterr().out
        assert '跳过' in out
        assert '10 没有上行指示灯' in out

    @pytest.mark.asyncio
    async def test_invalid_down_floor_skipped(self, app_and_console, capsys):
        """/buttonui out 1 down true → 1 没下行,应跳过 + 警告"""
        a, c = app_and_console
        await c._buttonui_out(['1', 'down', 'true'])
        out = capsys.readouterr().out
        assert '跳过' in out
        assert '1 没有下行指示灯' in out

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_up(self, app_and_console, capsys):
        """/buttonui out 1,5,10 up → 1、5 亮,10 跳过"""
        a, c = app_and_console
        await c._buttonui_out(['1,5,10', 'up', 'true'])
        out = capsys.readouterr().out
        assert '跳过' in out  # 10 被警告
        assert a.hall_indicator_state(1, 'up') is True
        assert a.hall_indicator_state(5, 'up') is True
        # 10 不会有 up 信号(被跳过了)

    @pytest.mark.asyncio
    async def test_all_invalid_errors(self, app_and_console, capsys):
        """/buttonui out 10 up → 全部不合法,报"无合法目标"错误"""
        a, c = app_and_console
        await c._buttonui_out(['10', 'up', 'true'])
        out = capsys.readouterr().out
        # 单元素列表,跳过 10 后 valid_floors 空 → 报错
        assert '错误' in out
        assert '没有合法目标' in out

    @pytest.mark.asyncio
    async def test_off_state(self, app_and_console):
        """先开再关"""
        a, c = app_and_console
        await c._buttonui_out(['1,3,5', 'up', 'true'])
        await c._buttonui_out(['1,3,5', 'up', 'false'])
        for floor in [1, 3, 5]:
            assert a.hall_indicator_state(floor, 'up') is False

    @pytest.mark.asyncio
    async def test_toggle_independently(self, app_and_console):
        """省略 true/false 时,逐个 toggle 自身状态"""
        a, c = app_and_console
        # 设 1 亮、3 灭
        await c._buttonui_out(['1', 'up', 'true'])
        await c._buttonui_out(['3', 'up', 'false'])
        assert a.hall_indicator_state(1, 'up') is True
        assert a.hall_indicator_state(3, 'up') is False

        # /buttonui out 1,3 up (toggle)
        await c._buttonui_out(['1,3', 'up'])
        assert a.hall_indicator_state(1, 'up') is False  # 翻转
        assert a.hall_indicator_state(3, 'up') is True   # 翻转

    @pytest.mark.asyncio
    async def test_io_actually_written_for_hall(self, app_and_console):
        """验证 IO 实际写到对应 DB 地址"""
        a, c = app_and_console
        await c._buttonui_out(['1,3', 'up', 'true'])
        # hall indicator 是 car_id=0 的全局信号,写在 self.io 上
        addr1 = a.mapper.addr_output('hall_indicator_up_1', 0)
        addr3 = a.mapper.addr_output('hall_indicator_up_3', 0)
        assert a.io.get_output(addr1) == 1
        assert a.io.get_output(addr3) == 1

        await c._buttonui_out(['1', 'up', 'false'])
        assert a.io.get_output(addr1) == 0
        assert a.io.get_output(addr3) == 1  # 3 不动