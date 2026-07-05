"""
test_display.py —— DisplayEncoder 单测
"""
from pathlib import Path

import pytest

from core.display import DisplayEncoder

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'display_config.yaml'


@pytest.fixture
def encoder() -> DisplayEncoder:
    return DisplayEncoder(CONFIG_PATH)


class TestSegments:
    def test_segments_loaded(self, encoder):
        assert 'a' in encoder.segments
        assert 'g' in encoder.segments
        assert 'm' in encoder.segments
        assert 'n' in encoder.segments  # 1 号桥专属 14 段
        assert len(encoder.segments) == 14


class TestGlyphs:
    def test_digit_0(self, encoder):
        assert encoder.get_segments_for_glyph('0') == {'a', 'b', 'c', 'd', 'e', 'f'}

    def test_digit_1(self, encoder):
        assert encoder.get_segments_for_glyph('1') == {'b', 'c'}

    def test_digit_8_all_seven(self, encoder):
        assert encoder.get_segments_for_glyph('8') == {'a', 'b', 'c', 'd', 'e', 'f', 'g'}

    def test_digit_9(self, encoder):
        assert encoder.get_segments_for_glyph('9') == {'a', 'b', 'c', 'd', 'f', 'g'}

    def test_up_arrow(self, encoder):
        assert encoder.get_segments_for_glyph('up') == {'a', 'b', 'e', 'f'}

    def test_down_arrow(self, encoder):
        assert encoder.get_segments_for_glyph('down') == {'d', 'e', 'f', 'g'}

    def test_blank(self, encoder):
        assert encoder.get_segments_for_glyph('blank') == set()

    def test_unknown_glyph_raises(self, encoder):
        with pytest.raises(ValueError, match='Z'):
            encoder.get_segments_for_glyph('Z')


class TestFloorDisplay:
    def test_floor_1_to_9(self, encoder):
        for f in range(1, 10):
            assert encoder.get_glyph_for_floor(f) == str(f)

    def test_floor_10_default_to_0(self, encoder):
        # 默认 10 楼显示 '0'（display_config.yaml 可改）
        assert encoder.get_glyph_for_floor(10) == '0'

    def test_floor_segments(self, encoder):
        # 1 楼 → 字符 '1' → 笔画 b, c
        assert encoder.get_segments_for_floor(1) == {'b', 'c'}
        # 5 楼 → 字符 '5' → 笔画 a, c, d, f, g
        assert encoder.get_segments_for_floor(5) == {'a', 'c', 'd', 'f', 'g'}
        # 10 楼 → 十位 '1'(i,j) + 个位 '0'(a,b,c,d,e,f)
        assert encoder.get_segments_for_floor(10) == {'a', 'b', 'c', 'd', 'e', 'f', 'i', 'j'}

    def test_undefined_floor_raises(self, encoder):
        with pytest.raises(ValueError, match='楼层'):
            encoder.get_glyph_for_floor(99)


class TestReload:
    def test_reload_picks_up_changes(self, encoder, tmp_path):
        # 写一个新 config，改 10 楼为 'A'
        new_cfg = tmp_path / 'display.yaml'
        new_cfg.write_text('''
segments: [a, b, c, d, e, f, g]
glyphs:
  '0': [a, b, c, d, e, f]
  '1': [b, c]
  'A': [a, b, c, e, f, g]
floor_display:
  10: 'A'
''', encoding='utf-8')

        encoder.config_path = new_cfg
        encoder.reload()

        assert encoder.get_glyph_for_floor(10) == 'A'
        # 'A' 走 a-g + 十位 '1' 走 i,j
        assert encoder.get_segments_for_floor(10) == {'a', 'b', 'c', 'e', 'f', 'g', 'i', 'j'}