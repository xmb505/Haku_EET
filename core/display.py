"""
display.py —— 7 段数码管编码 + IO 写入

从 config/display_config.yaml 读字符 → 笔画映射 + 楼层 → 字符映射。
比赛现场想改 10 楼显示、加新字符，都只动 config 文件 + /reload。

上层只需要调用 show_number(floor, car_id)，无需关心 segment 名和 IO 地址。
"""

from pathlib import Path
from typing import TYPE_CHECKING, Set

import yaml

if TYPE_CHECKING:
    from .io_client import IOClient
    from .io_mapper import IOMapper


class DisplayEncoder:
    def __init__(self, config_path: str | Path,
                 io: 'IOClient | None' = None,
                 mapper: 'IOMapper | None' = None) -> None:
        self.config_path = Path(config_path)
        self.io = io
        self.mapper = mapper
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

    # ---- 核心 IO API：传入数字，直接写 IO ---- #

    async def show_number(self, number: int, car_id: int) -> None:
        """显示一个数字 → 自动计算笔画 → 写 IO（经 tick 缓冲区）"""
        await self._write_segments(self.number_to_segments(number), car_id)

    async def show_glyph(self, glyph_name: str, car_id: int) -> None:
        """显示一个命名字符（up/down/fault/blank 等），十位补 0"""
        glyph_seg = self.get_segments_for_glyph(glyph_name)
        full = self.number_to_segments(0).union(glyph_seg)
        await self._write_segments(full, car_id)

    async def clear_display(self, car_id: int) -> None:
        """全灭"""
        await self._write_segments(set(), car_id)

    async def _write_segments(self, segments: Set[str], car_id: int) -> None:
        """笔画集合 → 写 IO（经 tick 缓冲区）"""
        if self.io is None or self.mapper is None:
            return
        writes: dict[str, int] = {}
        for seg in set(self.segments):
            try:
                addr = self.mapper.addr_output(f'segment_{seg}', car_id)
                writes[addr] = 1 if seg in segments else 0
            except KeyError:
                continue
        if writes:
            await self.io.set_many(writes)

    def get_glyph_for_floor(self, floor: int) -> str:
        """楼层 → 显示字符"""
        if floor not in self.floor_display:
            raise ValueError(
                f'楼层 {floor} 没有定义显示规则，请检查 display_config.yaml 的 floor_display'
            )
        return self.floor_display[floor]

    # ---- 核心 API：数字 → 笔画 ---- #

    # 第 N 位（0=最右）的 7 段管笔画偏移
    #   pos=0 → a-g（个位）
    #   pos=1 → h-n（十位）
    #   pos=2 → o-v（百位，预留）
    _SEGMENT_OFFSETS = [
        str.maketrans('abcdefg', 'hijklmn'),  # pos=1
        str.maketrans('abcdefg', 'opqrstv'),  # pos=2
    ]

    def number_to_segments(self, number: int) -> Set[str]:
        """任意数字 → 笔画名集合（自动分配多位 7 段管）"""
        segments: Set[str] = set()
        for pos, ch in enumerate(reversed(str(number).zfill(2))):
            seg = self.get_segments_for_glyph(ch)
            if pos > 0:
                seg = {s.translate(self._SEGMENT_OFFSETS[pos - 1]) for s in seg}
            segments.update(seg)
        return segments

    # ---- 保留接口（委托给 number_to_segments） ---- #

    def get_segments_for_floor(self, floor: int) -> Set[str]:
        """楼层 → 笔画名集合（兼容旧接口：使用 floor_display 做字符映射）"""
        glyph = self.get_glyph_for_floor(floor)
        # 标准数字字符（0-9）→ 正常多位数格式化
        if glyph.isdigit() and len(glyph) == 1:
            return self.number_to_segments(floor)
        # 自定义字符（A、F、E 等）→ 字符显示在个位（a-g），十位从楼层数字计算
        units = self.get_segments_for_glyph(glyph)
        tens_glyph = str(floor)[-2] if len(str(floor)) >= 2 else '0'
        tens_seg = self.get_segments_for_glyph(tens_glyph)
        tens = {s.translate(self._SEGMENT_OFFSETS[0]) for s in tens_seg}
        return tens.union(units)

    def get_units_segments(self, floor: int) -> Set[str]:
        """个位数笔画（a-g），兼容旧调用"""
        return self.get_segments_for_glyph(str(floor)[-1])

    def get_tens_segments(self, floor: int) -> Set[str]:
        """十位数笔画（h-n），兼容旧调用"""
        ch = str(floor)[-2] if len(str(floor)) >= 2 else '0'
        seg = self.get_segments_for_glyph(ch)
        return {s.translate(self._SEGMENT_OFFSETS[0]) for s in seg}

    def _digit_to_segments(self, digit: int) -> Set[str]:
        """数字的一位（0-9）→ 字符 → 笔画（a-g）"""
        return self.get_segments_for_glyph(str(digit))

    def get_segments_for_glyph(self, glyph: str) -> Set[str]:
        """字符 → 笔画名集合"""
        if glyph not in self.glyphs:
            raise ValueError(f'字符 {glyph!r} 没有定义笔画编码，请检查 display_config.yaml 的 glyphs')
        return set(self.glyphs[glyph])

    def all_segments_off(self) -> Set[str]:
        """全灭（清除显示）"""
        return set()