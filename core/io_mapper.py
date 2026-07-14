"""
io_mapper.py —— IO 地址映射

职责:
    - 加载 config/io_config.yaml
    - 提供 逻辑信号名 + car_id → 地址 的查表
    - 输入为 I 地址（对应 IO2HTTP WebSocket bitmap）
    - 输出为 DB11.DBX 地址（对应 HTTP 写 DB 区）

完全不知道 Car / Action / 算法逻辑，只搬运地址。
"""

from pathlib import Path
from typing import Union

import yaml


class IOMapper:

    def __init__(self, config_path: Union[str, Path]) -> None:
        self.config_path = Path(config_path)
        self._output_cache: dict[tuple[int, str], str] = {}
        self._input_cache: dict[tuple[int, str], str] = {}
        # 反向索引：地址 → (car_id, signal_name)
        self._output_db_to_signal: dict[str, tuple[int, str]] = {}
        self._input_db_to_signal: dict[str, tuple[int, str]] = {}
        # I 地址 → (car_id, signal_name) —— WebSocket bitmap 事件反查
        self._i_to_signal: dict[str, tuple[int, str]] = {}
        self.reload()

    def reload(self) -> None:
        with self.config_path.open('r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        self._output_cache.clear()
        self._input_cache.clear()
        self._output_db_to_signal.clear()
        self._input_db_to_signal.clear()
        self._i_to_signal.clear()

        # 输出（DB 地址，直接写 PLC DB11）
        output = cfg['output']
        per_car_out = output.get('per_car', {})
        for car_id_str, sigs in per_car_out.items():
            car_id = int(car_id_str)
            for sig, db_addr in sigs.items():
                self._output_cache[(car_id, sig)] = db_addr
                self._output_db_to_signal[db_addr] = (car_id, sig)
        for sig, db_addr in output.get('hall_indicator', {}).items():
            self._output_cache[(0, sig)] = db_addr
            self._output_db_to_signal[db_addr] = (0, sig)
        if 'ready' in output:
            self._output_cache[(0, 'ready')] = output['ready']
            self._output_db_to_signal[output['ready']] = (0, 'ready')

        # 输入（I 地址，来自 IO2HTTP WebSocket bitmap）
        input_cfg = cfg['input']
        per_car_in = input_cfg.get('per_car', {})
        for car_id_str, sigs in per_car_in.items():
            car_id = int(car_id_str)
            for sig, i_addr in sigs.items():
                self._input_cache[(car_id, sig)] = i_addr
                self._input_db_to_signal[i_addr] = (car_id, sig)
                self._i_to_signal[i_addr] = (car_id, sig)
        for sig, i_addr in input_cfg.get('hall_call', {}).items():
            self._input_cache[(0, sig)] = i_addr
            self._input_db_to_signal[i_addr] = (0, sig)
            self._i_to_signal[i_addr] = (0, sig)
        if 'auto_run' in input_cfg:
            addr = input_cfg['auto_run']
            self._input_cache[(0, 'auto_run')] = addr
            self._input_db_to_signal[addr] = (0, 'auto_run')
            self._i_to_signal[addr] = (0, 'auto_run')

    # ===== 查表 =====

    def addr_output(self, signal: str, car_id: int = 1) -> str:
        """输出信号: 逻辑名 + car_id → DB 地址（HTTP 写 PLC DB11 用）"""
        key = (car_id, signal)
        if key not in self._output_cache:
            raise KeyError(f'未知输出信号: car_id={car_id} signal={signal!r}')
        return self._output_cache[key]

    def addr_input(self, signal: str, car_id: int = 1) -> str:
        """输入信号: 逻辑名 + car_id → I 地址（IO2HTTP WebSocket bitmap 用）"""
        key = (car_id, signal)
        if key not in self._input_cache:
            raise KeyError(f'未知输入信号: car_id={car_id} signal={signal!r}')
        return self._input_cache[key]

    def lookup_signal_by_i(self, i_addr: str) -> tuple[int, str] | None:
        """WebSocket bitmap 事件反查: I 地址 → (car_id, signal_name)"""
        return self._i_to_signal.get(i_addr)

    def lookup_all_i_addresses(self) -> list[str]:
        """列出所有已知的 I 地址（IOClient bitmap 派发过滤用）"""
        return list(self._i_to_signal.keys())

    # ===== 给 REPL 用的辅助 =====

    def all_input_signals(self, car_id: int = 1) -> list[str]:
        return [sig for (cid, sig) in self._input_cache if cid == car_id]

    def all_output_signals(self, car_id: int = 1) -> list[str]:
        return [sig for (cid, sig) in self._output_cache if cid == car_id]