"""
io_mapper.py —— IO 地址映射

职责:
    - 加载 config/io_config.yaml
    - 提供 逻辑信号名 + car_id → DB 地址 的查表
    - 提供 DB ↔ I 地址的双向换算（订阅 WebSocket 用）

完全不知道 Car / Action / 算法逻辑，只搬运地址。
"""

import re
from pathlib import Path
from typing import Union

import yaml


class IOMapper:
    DB_ADDR_RE = re.compile(r'^DB(\d+)\.DBX(\d+)\.(\d+)$')
    I_ADDR_RE = re.compile(r'^I(\d+)\.(\d+)$')

    # PLC 程序约定的输入 DB 块号（点位表说输入在 DB10，主动推到 I 区）
    INPUT_DB_NUMBER = 10

    def __init__(self, config_path: Union[str, Path]) -> None:
        self.config_path = Path(config_path)
        self.db_to_i_offset: int = 0
        self._output_cache: dict[tuple[int, str], str] = {}
        self._input_cache: dict[tuple[int, str], str] = {}
        # 反向索引：db_addr (输出) → (car_id, signal_name) —— 用于按地址反查
        self._output_db_to_signal: dict[str, tuple[int, str]] = {}
        self._input_db_to_signal: dict[str, tuple[int, str]] = {}
        # 输入 I 地址 → (car_id, signal_name) —— WebSocket 事件反查用
        self._i_to_signal: dict[str, tuple[int, str]] = {}
        self.reload()

    def reload(self) -> None:
        with self.config_path.open('r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        self.db_to_i_offset = int(cfg['db_to_i_offset'])
        self._output_cache.clear()
        self._input_cache.clear()
        self._output_db_to_signal.clear()
        self._input_db_to_signal.clear()
        self._i_to_signal.clear()

        # 输出
        output = cfg['output']
        per_car_out = output.get('per_car', {})
        for car_id_str, sigs in per_car_out.items():
            car_id = int(car_id_str)
            for sig, db_addr in sigs.items():
                self._output_cache[(car_id, sig)] = db_addr
                self._output_db_to_signal[db_addr] = (car_id, sig)
        # 全局输出
        for sig, db_addr in output.get('hall_indicator', {}).items():
            self._output_cache[(0, sig)] = db_addr
            self._output_db_to_signal[db_addr] = (0, sig)
        if 'ready' in output:
            self._output_cache[(0, 'ready')] = output['ready']
            self._output_db_to_signal[output['ready']] = (0, 'ready')

        # 输入
        input_cfg = cfg['input']
        per_car_in = input_cfg.get('per_car', {})
        for car_id_str, sigs in per_car_in.items():
            car_id = int(car_id_str)
            for sig, db_addr in sigs.items():
                self._input_cache[(car_id, sig)] = db_addr
                self._input_db_to_signal[db_addr] = (car_id, sig)
                self._i_to_signal[self.db_to_i(db_addr)] = (car_id, sig)
        for sig, db_addr in input_cfg.get('hall_call', {}).items():
            self._input_cache[(0, sig)] = db_addr
            self._input_db_to_signal[db_addr] = (0, sig)
            self._i_to_signal[self.db_to_i(db_addr)] = (0, sig)
        if 'auto_run' in input_cfg:
            self._input_cache[(0, 'auto_run')] = input_cfg['auto_run']
            self._input_db_to_signal[input_cfg['auto_run']] = (0, 'auto_run')
            self._i_to_signal[self.db_to_i(input_cfg['auto_run'])] = (0, 'auto_run')

    # ===== 查表 =====

    def addr_output(self, signal: str, car_id: int = 1) -> str:
        """输出信号: 逻辑名 + car_id → DB 地址（硬件层写 IO 用）"""
        key = (car_id, signal)
        if key not in self._output_cache:
            raise KeyError(f'未知输出信号: car_id={car_id} signal={signal!r}')
        return self._output_cache[key]

    def addr_input(self, signal: str, car_id: int = 1) -> str:
        """输入信号: 逻辑名 + car_id → DB 地址（查询时用，主要订阅走 i_to_signal）"""
        key = (car_id, signal)
        if key not in self._input_cache:
            raise KeyError(f'未知输入信号: car_id={car_id} signal={signal!r}')
        return self._input_cache[key]

    def lookup_signal_by_i(self, i_addr: str) -> tuple[int, str] | None:
        """WebSocket 事件反查: I 地址 → (car_id, signal_name)，未找到返回 None"""
        return self._i_to_signal.get(i_addr)

    # ===== DB ↔ I 换算 =====

    def db_to_i(self, db_addr: str) -> str:
        """DB 地址 → I 地址（订阅 WebSocket 时需要 I 地址）"""
        m = self.DB_ADDR_RE.match(db_addr)
        if not m:
            raise ValueError(f'不是合法的 DB 地址: {db_addr!r}')
        byte = int(m.group(2))
        bit = int(m.group(3))
        i_byte = byte + self.db_to_i_offset
        return f'I{i_byte}.{bit}'

    def i_to_db(self, i_addr: str) -> str:
        """I 地址 → DB 地址（WebSocket 事件反查时用，输入默认来自 DB10）"""
        m = self.I_ADDR_RE.match(i_addr)
        if not m:
            raise ValueError(f'不是合法的 I 地址: {i_addr!r}')
        i_byte = int(m.group(1))
        bit = int(m.group(2))
        byte = i_byte - self.db_to_i_offset
        if byte < 0:
            raise ValueError(f'I 地址 {i_addr} 偏移后 byte={byte} 越界（offset={self.db_to_i_offset}）')
        return f'DB{self.INPUT_DB_NUMBER}.DBX{byte}.{bit}'

    # ===== 给 REPL 用的辅助 =====

    def all_input_signals(self, car_id: int = 1) -> list[str]:
        """列出某轿厢的所有输入信号名（/io dump 用）"""
        return [sig for (cid, sig) in self._input_cache if cid == car_id]

    def all_output_signals(self, car_id: int = 1) -> list[str]:
        return [sig for (cid, sig) in self._output_cache if cid == car_id]