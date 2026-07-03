#!/usr/bin/env python3
"""
gen_io_config.py —— 从点位表.md 解析并生成 io_config.yaml

用法:
    python3 tools/gen_io_config.py
    python3 tools/gen_io_config.py --input 点位表.md --output config/io_config.yaml

点位表改了之后，重跑这个脚本即可，io_mapper 不需要改。
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

# 已知虚假点位（点位表里有但硬件实际没接，解析时直接丢弃）
# 历史背景：老版点位表里每部桥都有一个"门锁指示灯"（DB11.DBX{3+n}.4），
# 实际该地址是段码 LEDa，所以是文档错误
SKIP_SIGNALS: set[str] = {
    '1号桥门锁指示灯', '2号桥门锁指示灯', '3号桥门锁指示灯',
    '4号桥门锁指示灯', '5号桥门锁指示灯', '6号桥门锁指示灯',
}


# 信号名翻译表（按顺序匹配，第一个匹配上的就用）
# 每轿厢通用信号 + 全局信号
TRANSLATIONS: List[Tuple[str, str]] = [
    # ===== 输入 =====
    (r'(\d+)层上行召梯按钮', r'hall_call_up_\1'),
    (r'(\d+)层下行召梯按钮', r'hall_call_down_\1'),
    (r'(\d+)号桥轿内选层按钮(\d+)', r'cabin_button_\2'),
    (r'(\d+)号桥轿内开门按钮', r'door_open_button'),
    (r'(\d+)号桥轿内关门按钮', r'door_close_button'),
    (r'(\d+)号桥光幕信号', r'light_curtain'),
    (r'(\d+)号桥超重信号', r'overload'),
    (r'(\d+)号桥检修信号', r'service_mode'),
    (r'(\d+)号桥轿厢门门锁信号', r'car_door_lock'),
    (r'(\d+)号桥(\d+)楼层门门锁信号', r'floor_door_lock_\2'),
    (r'(\d+)号桥开门到位', r'door_open_done'),
    (r'(\d+)号桥关门到位', r'door_close_done'),
    (r'(\d+)号桥上平层信号', r'level_up'),
    (r'(\d+)号桥下平层信号', r'level_down'),
    (r'(\d+)号桥上端站第(\d+)限位', r'top_limit_\2'),
    (r'(\d+)号桥下端站第(\d+)限位', r'bottom_limit_\2'),
    # ===== 输出 =====
    (r'(\d+)层上行呼梯按钮指示灯', r'hall_indicator_up_\1'),
    (r'(\d+)层下行呼梯按钮指示灯', r'hall_indicator_down_\1'),
    (r'(\d+)号桥(\d+)层按钮指示灯', r'cabin_button_led_\2'),
    # 注：原版点位表里的"门锁指示灯"是虚假点位（DB11.DBX{3+n}.4 实际是段码 a）
    # 新版点位表用 LED 命名，1 号桥有完整 14 段 (a-n)，其他桥 13 段 (a-m)
    # 兼容老点位表的 ED 命名（少 1 段，从 DB11.DBX{3+n}.5 开始）
    (r'(\d+)号桥LED([a-m])', r'segment_\2'),
    (r'(\d+)号桥LEDn', r'segment_n'),
    (r'(\d+)号桥ED([a-m])', r'segment_\2'),
    (r'(\d+)号桥上行指示', r'up_indicator'),
    (r'(\d+)号桥下行指示', r'down_indicator'),
    (r'(\d+)号桥故障指示', r'fault_indicator'),
    (r'(\d+)号桥照明指示', r'light_indicator'),
    (r'(\d+)号桥风扇指示', r'fan_indicator'),
    (r'(\d+)号桥满载指示', r'full_load_indicator'),
    (r'(\d+)号桥电机启动信号', r'motor_start'),
    (r'(\d+)号桥上行接触器', r'up_contactor'),
    (r'(\d+)号桥下行接触器', r'down_contactor'),
    (r'(\d+)号桥高速接触器', r'high_speed_contactor'),
    (r'(\d+)号桥低速接触器', r'low_speed_contactor'),
    (r'(\d+)号桥开门门继电器', r'door_open_relay'),
    (r'(\d+)号桥关门门继电器', r'door_close_relay'),
    (r'(\d+)号桥(\d+)级减速制动', r'brake_\2'),
    # ===== 全局 =====
    (r'自动运行信号', 'auto_run'),
    (r'准备就绪信号', 'ready'),
]


def translate_signal_name(raw: str) -> str:
    """把点位表里的位号翻译成逻辑信号名（snake_case）"""
    raw = raw.strip()
    for pattern, replacement in TRANSLATIONS:
        m = re.match(pattern, raw)
        if m:
            return m.expand(replacement)
    # 兜底：未匹配的信号就用位号原文（去空格、snake_case）
    fallback = re.sub(r'\s+', '_', raw)
    print(f'  WARN: 未匹配翻译规则，使用原文: {raw!r} → {fallback}', file=sys.stderr)
    return fallback


def parse_point_table(path: Path) -> Tuple[Dict, Dict]:
    """
    解析点位表.md，返回 (input_dict, output_dict)

    input_dict/output_dict 结构:
        {
            'hall_call': {signal_name: db_addr, ...},  # 全局大厅输入（仅输入有）
            'hall_indicator': {signal_name: db_addr, ...},  # 全局大厅输出（仅输出有）
            'per_car': {
                '1': {signal_name: db_addr, ...},
                '2': {...},
                ...
            },
            'auto_run': db_addr,  # 仅输入
            'ready': db_addr,     # 仅输出
        }
    """
    text = path.read_text(encoding='utf-8')

    # 定位输入/输出区块
    input_match = re.search(r'\| 输入\s*\|.*?\n([\s\S]*?)(?=\n\s*\n|\| 输出)', text)
    output_match = re.search(r'\| 输出\s*\|.*?\n([\s\S\S]*?)(?=\n\s*\n---|\Z)', text)

    if not input_match or not output_match:
        raise RuntimeError('点位表格式异常：找不到"输入"或"输出"区块')

    # 行格式: | 序号 | 位号 | （相对）地址 | 数据类型 |
    row_re = re.compile(r'\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*(DB\d+\.DBX\d+\.\d+)\s*\|\s*\w+\s*\|')

    def parse_rows(block: str) -> List[Tuple[str, str]]:
        rows = []
        for line in block.splitlines():
            m = row_re.search(line)
            if m:
                rows.append((m.group(2).strip(), m.group(3).strip()))
        return rows

    input_rows = parse_rows(input_match.group(1))
    output_rows = parse_rows(output_match.group(1))

    print(f'  解析输入行: {len(input_rows)} 条')
    print(f'  解析输出行: {len(output_rows)} 条')

    # 过滤掉已知的虚假点位
    before = len(output_rows)
    output_rows = [(name, addr) for name, addr in output_rows if name not in SKIP_SIGNALS]
    skipped = before - len(output_rows)
    if skipped:
        print(f'  跳过 {skipped} 个虚假点位（门锁指示灯 → 实际是段码 LEDa）')

    input_dict: Dict = {'hall_call': {}, 'per_car': {}}
    output_dict: Dict = {'hall_indicator': {}, 'per_car': {}}

    # 提取轿厢 ID（"N号桥"）的正则
    car_re = re.compile(r'^(\d+)号桥')

    for raw_name, db_addr in input_rows:
        sig = translate_signal_name(raw_name)
        m = car_re.match(raw_name)
        if m:
            car_id = m.group(1)
            input_dict['per_car'].setdefault(car_id, {})[sig] = db_addr
        elif sig in ('auto_run',):
            input_dict['auto_run'] = db_addr
        else:
            input_dict['hall_call'][sig] = db_addr

    for raw_name, db_addr in output_rows:
        sig = translate_signal_name(raw_name)
        m = car_re.match(raw_name)
        if m:
            car_id = m.group(1)
            output_dict['per_car'].setdefault(car_id, {})[sig] = db_addr
        elif sig in ('ready',):
            output_dict['ready'] = db_addr
        else:
            output_dict['hall_indicator'][sig] = db_addr

    return input_dict, output_dict


def gen_io_config(point_table: Path, output: Path) -> None:
    print(f'解析点位表: {point_table}')
    input_dict, output_dict = parse_point_table(point_table)

    # db_to_i_offset = 2（DB10.DBX0.0 → I2.0，由 IO2HTTP 文档约定）
    config = {
        'db_to_i_offset': 2,
        'input': input_dict,
        'output': output_dict,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f'已生成: {output}')
    print(f'  全局大厅输入信号: {len(input_dict["hall_call"])}')
    print(f'  全局大厅输出信号: {len(output_dict["hall_indicator"])}')
    for car_id, sigs in sorted(input_dict['per_car'].items()):
        print(f'  轿厢 {car_id}: 输入 {len(sigs)} 条')
    for car_id, sigs in sorted(output_dict['per_car'].items()):
        print(f'  轿厢 {car_id}: 输出 {len(sigs)} 条')


def main():
    parser = argparse.ArgumentParser(description='从点位表生成 io_config.yaml')
    parser.add_argument(
        '--input', '-i',
        type=Path,
        default=Path(__file__).parent.parent / '点位表.md',
        help='点位表路径（默认: 项目根目录/点位表.md）',
    )
    parser.add_argument(
        '--output', '-o',
        type=Path,
        default=Path(__file__).parent.parent / 'config' / 'io_config.yaml',
        help='输出 yaml 路径（默认: config/io_config.yaml）',
    )
    args = parser.parse_args()
    gen_io_config(args.input, args.output)


if __name__ == '__main__':
    main()