"""
test_io_mapper.py —— IO 地址映射单测
"""
from pathlib import Path

import pytest

from core.io_mapper import IOMapper

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'io_config.yaml'


@pytest.fixture
def mapper() -> IOMapper:
    return IOMapper(CONFIG_PATH)


class TestLoad:
    def test_car1_outputs_loaded(self, mapper):
        assert mapper.addr_output('up_contactor', 1) == 'DB11.DBX6.1'
        assert mapper.addr_output('down_contactor', 1) == 'DB11.DBX6.2'
        assert mapper.addr_output('motor_start', 1) == 'DB11.DBX6.0'

    def test_car1_inputs_loaded(self, mapper):
        # addr_input 现在直接返回 I 地址（DB10.DBX +2 偏移）
        assert mapper.addr_input('level_up', 1) == 'I7.6'
        assert mapper.addr_input('bottom_limit_1', 1) == 'I8.2'
        assert mapper.addr_input('door_open_done', 1) == 'I7.4'

    def test_hall_outputs_loaded(self, mapper):
        assert mapper.addr_output('hall_indicator_up_1', 0) == 'DB11.DBX0.0'
        assert mapper.addr_output('hall_indicator_down_2', 0) == 'DB11.DBX1.1'

    def test_hall_inputs_loaded(self, mapper):
        assert mapper.addr_input('hall_call_up_1', 0) == 'I2.0'
        assert mapper.addr_input('hall_call_down_10', 0) == 'I4.1'

    def test_ready_and_auto_run(self, mapper):
        assert mapper.addr_output('ready', 0) == 'DB11.DBX32.2'
        assert mapper.addr_input('auto_run', 0) == 'I29.6'

    def test_segments_loaded(self, mapper):
        assert mapper.addr_output('segment_a', 1) == 'DB11.DBX3.4'
        assert mapper.addr_output('segment_m', 1) == 'DB11.DBX5.0'
        assert mapper.addr_output('segment_n', 1) == 'DB11.DBX5.1'

    def test_unknown_signal_raises(self, mapper):
        with pytest.raises(KeyError):
            mapper.addr_output('does_not_exist', 1)
        with pytest.raises(KeyError):
            mapper.addr_input('does_not_exist', 1)

    def test_unknown_car_id_raises(self, mapper):
        with pytest.raises(KeyError):
            mapper.addr_output('up_contactor', 99)


class TestSignalLookup:
    def test_lookup_by_i(self, mapper):
        assert mapper.lookup_signal_by_i('I4.2') == (1, 'cabin_button_1')

    def test_lookup_by_i_hall(self, mapper):
        assert mapper.lookup_signal_by_i('I2.0') == (0, 'hall_call_up_1')

    def test_lookup_unknown_returns_none(self, mapper):
        assert mapper.lookup_signal_by_i('I99.0') is None


class TestReload:
    def test_reload_picks_up_changes(self, mapper, tmp_path):
        new_cfg = tmp_path / 'io.yaml'
        new_cfg.write_text('''
input:
  per_car:
    '1':
      test_signal: I2.0
output:
  per_car:
    '1':
      test_out: DB11.DBX0.0
''', encoding='utf-8')

        mapper.config_path = new_cfg
        mapper.reload()

        assert mapper.addr_input('test_signal', 1) == 'I2.0'
        assert mapper.addr_output('test_out', 1) == 'DB11.DBX0.0'
