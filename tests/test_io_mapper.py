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
    def test_loads_offset(self, mapper):
        assert mapper.db_to_i_offset == 2

    def test_car1_outputs_loaded(self, mapper):
        assert mapper.addr_output('up_contactor', 1) == 'DB11.DBX6.1'
        assert mapper.addr_output('down_contactor', 1) == 'DB11.DBX6.2'
        assert mapper.addr_output('motor_start', 1) == 'DB11.DBX6.0'

    def test_car1_inputs_loaded(self, mapper):
        assert mapper.addr_input('level_up', 1) == 'DB10.DBX5.6'
        assert mapper.addr_input('bottom_limit_1', 1) == 'DB10.DBX6.2'
        assert mapper.addr_input('door_open_done', 1) == 'DB10.DBX5.4'

    def test_hall_outputs_loaded(self, mapper):
        assert mapper.addr_output('hall_indicator_up_1', 0) == 'DB11.DBX0.0'
        assert mapper.addr_output('hall_indicator_down_2', 0) == 'DB11.DBX1.1'

    def test_hall_inputs_loaded(self, mapper):
        assert mapper.addr_input('hall_call_up_1', 0) == 'DB10.DBX0.0'
        assert mapper.addr_input('hall_call_down_10', 0) == 'DB10.DBX2.1'

    def test_ready_and_auto_run(self, mapper):
        assert mapper.addr_output('ready', 0) == 'DB11.DBX32.2'
        assert mapper.addr_input('auto_run', 0) == 'DB10.DBX27.6'

    def test_segments_loaded(self, mapper):
        # 7 段数码管，1 号桥 14 段 (a-n)，其他桥 13 段 (a-m)
        assert mapper.addr_output('segment_a', 1) == 'DB11.DBX3.4'
        assert mapper.addr_output('segment_m', 1) == 'DB11.DBX5.0'
        # 1 号桥专属 LEDn
        assert mapper.addr_output('segment_n', 1) == 'DB11.DBX5.1'

    def test_unknown_signal_raises(self, mapper):
        with pytest.raises(KeyError):
            mapper.addr_output('does_not_exist', 1)
        with pytest.raises(KeyError):
            mapper.addr_input('does_not_exist', 1)

    def test_unknown_car_id_raises(self, mapper):
        with pytest.raises(KeyError):
            mapper.addr_output('up_contactor', 99)


class TestDbToI:
    def test_basic_offset(self, mapper):
        # DB10.DBX0.0 → I2.0
        assert mapper.db_to_i('DB10.DBX0.0') == 'I2.0'

    def test_byte_offset(self, mapper):
        # DB10.DBX2.2 → I4.2
        assert mapper.db_to_i('DB10.DBX2.2') == 'I4.2'

    def test_end_of_byte(self, mapper):
        # DB10.DBX1.7 → I3.7
        assert mapper.db_to_i('DB10.DBX1.7') == 'I3.7'

    def test_invalid_raises(self, mapper):
        with pytest.raises(ValueError):
            mapper.db_to_i('Q0.0')
        with pytest.raises(ValueError):
            mapper.db_to_i('garbage')


class TestIToDb:
    def test_basic(self, mapper):
        # I2.0 → DB10.DBX0.0
        assert mapper.i_to_db('I2.0') == 'DB10.DBX0.0'

    def test_offset(self, mapper):
        assert mapper.i_to_db('I4.2') == 'DB10.DBX2.2'

    def test_roundtrip(self, mapper):
        for db in ['DB10.DBX0.0', 'DB10.DBX2.2', 'DB10.DBX5.6', 'DB10.DBX27.6']:
            assert mapper.i_to_db(mapper.db_to_i(db)) == db

    def test_underflow_raises(self, mapper):
        with pytest.raises(ValueError):
            mapper.i_to_db('I1.0')  # offset=2，I1 → byte=-1

    def test_invalid_raises(self, mapper):
        with pytest.raises(ValueError):
            mapper.i_to_db('Q0.0')


class TestSignalLookup:
    def test_lookup_by_i(self, mapper):
        # I4.2 = DB10.DBX2.2 = car 1 的 cabin_button_1
        assert mapper.lookup_signal_by_i('I4.2') == (1, 'cabin_button_1')

    def test_lookup_by_i_hall(self, mapper):
        # I2.0 = DB10.DBX0.0 = hall_call_up_1 (car_id=0)
        assert mapper.lookup_signal_by_i('I2.0') == (0, 'hall_call_up_1')

    def test_lookup_unknown_returns_none(self, mapper):
        assert mapper.lookup_signal_by_i('I99.0') is None


class TestReload:
    def test_reload_picks_up_changes(self, mapper, tmp_path):
        new_cfg = tmp_path / 'io.yaml'
        new_cfg.write_text('''
db_to_i_offset: 3
input:
  per_car:
    '1':
      test_signal: DB10.DBX0.0
output:
  per_car:
    '1':
      test_out: DB11.DBX0.0
''', encoding='utf-8')

        mapper.config_path = new_cfg
        mapper.reload()

        assert mapper.db_to_i_offset == 3
        assert mapper.addr_input('test_signal', 1) == 'DB10.DBX0.0'
        assert mapper.db_to_i('DB10.DBX0.0') == 'I3.0'