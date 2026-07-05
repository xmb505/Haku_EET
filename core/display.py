"""
display.py —— 7 段数码管编码查表

从 config/display_config.yaml 读字符 → 笔画映射 + 楼层 → 字符映射。
比赛现场想改 10 楼显示、加新字符，都只动 config 文件 + /reload。

完全独立：不知道 IO 地址，不知道 car_id，硬件层 executor 拿笔画名后再查 io_mapper。
"""

from pathlib import Path
from typing import Set

import yaml


class DisplayEncoder:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.segments: list[str] = []
        self.glyphs: dict[str, list[str]] = {}
        self.floor_display: dict[int, str] = {}
        self.reload()

    def reload(self) -> None:
        """重新读 config（用于 /reload 命令）"""
        with self.config_path.open('r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        self.segments = list(cfg['segments'])
        self.glyphs = {k: list(v) for k, v in cfg['glyphs'].items()}
        self.floor_display = {int(k): str(v) for k, v in cfg['floor_display'].items()}

        # 校验：所有 glyph 引用的笔画都必须在 segments 里
        all_segs: Set[str] = set(self.segments)
        for name, segs in self.glyphs.items():
            unknown = set(segs) - all_segs
            if unknown:
                raise ValueError(
                    f'glyph {name!r} 引用了未定义的笔画: {unknown}，'
                    f'请在 display_config.yaml 的 segments 里补齐'
                )

        # 校验：所有 floor_display 引用的 glyph 都必须存在
        for floor, glyph in self.floor_display.items():
            if glyph not in self.glyphs:
                raise ValueError(
                    f'floor {floor} 映射到字符 {glyph!r}，但 glyphs 里没有该字符，'
                    f'请在 display_config.yaml 的 glyphs 里补齐'
                )

    def get_glyph_for_floor(self, floor: int) -> str:
        """楼层 → 显示字符"""
        if floor not in self.floor_display:
            raise ValueError(
                f'楼层 {floor} 没有定义显示规则，请检查 display_config.yaml 的 floor_display'
            )
        return self.floor_display[floor]

    def get_segments_for_floor(self, floor: int) -> Set[str]:
        """楼层 → 笔画名集合（十位 h-n + 个位 a-g）"""
        return self.get_tens_segments(floor).union(self.get_units_segments(floor))

    def get_units_segments(self, floor: int) -> Set[str]:
        """个位数笔画（a-g，低位 7 段管）"""
        return self.get_segments_for_glyph(self.get_glyph_for_floor(floor))

    def get_tens_segments(self, floor: int) -> Set[str]:
        """十位数笔画（h-n，高位 7 段管）；<10 楼返回空集"""
        if floor < 10:
            return set()
        seg = self.get_segments_for_glyph(str(floor // 10))
        # a→h, b→i, c→j, d→k, e→l, f→m, g→n
        shift = str.maketrans('abcdefg', 'hijklmn')
        return {s.translate(shift) for s in seg}

    def get_segments_for_glyph(self, glyph: str) -> Set[str]:
        """字符 → 笔画名集合"""
        if glyph not in self.glyphs:
            raise ValueError(f'字符 {glyph!r} 没有定义笔画编码，请检查 display_config.yaml 的 glyphs')
        return set(self.glyphs[glyph])

    def all_segments_off(self) -> Set[str]:
        """全灭（清除显示）"""
        return set()