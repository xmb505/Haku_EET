"""
logging.py —— 日志文件 + stderr 分流

策略：
- sys.stderr 整体替换为 TeeStderr → 所有 stderr 输出自动写文件 + 终端
- executor._log 额外走纯文件（文件始终写），终端受 exec_log_enabled 控制
"""

import sys
import os
import time
from datetime import datetime


class TimestampedFile:
    """每行写入自动加 [HH:MM:SS.mmm] 时间戳"""
    def __init__(self, file_obj):
        self._file = file_obj

    def write(self, msg: str) -> int:
        ts = datetime.now().strftime('%H:%M:%S.') + f'{int(time.time() * 1000) % 1000:03d}'
        if msg.endswith('\n'):
            self._file.write(f'[{ts}] {msg}')
        else:
            self._file.write(f'[{ts}] {msg}\n')
        self._file.flush()
        return len(msg)

    def flush(self) -> None:
        self._file.flush()

    @property
    def raw(self):
        """无时间戳的底层文件对象"""
        return self._file


class TeeStderr:
    """同时写入原始 stderr 和日志文件（带时间戳）"""
    def __init__(self, original_stderr, log_file) -> None:
        self._stderr = original_stderr
        self._file = log_file  # TimestampedFile 或普通 file

    def write(self, msg: str) -> int:
        self._stderr.write(msg)
        self._stderr.flush()
        self._file.write(msg)
        self._file.flush()
        return len(msg)

    def flush(self) -> None:
        self._stderr.flush()
        self._file.flush()

    def fileno(self) -> int:
        return self._stderr.fileno()

    def close(self) -> None:
        if hasattr(self._file, 'raw'):
            self._file.raw.close()
        else:
            self._file.close()


def init_log(log_dir: str = 'logs') -> tuple:
    """创建日志目录 + 文件，返回 (TeeStderr, 纯文件对象)

    TeeStderr 用于替换 sys.stderr → 所有 stderr 输出自动双写
    纯文件对象 用于 executor._log_stream → 文件始终写，终端受开关控制

    文件命名: YYYY-MM-DD-N.log（N 从 1 递增，类似 Minecraft 服务端）
    """
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    n = 1
    while True:
        filename = f'{today}-{n}.log'
        path = os.path.join(log_dir, filename)
        if not os.path.exists(path):
            break
        n += 1

    # 纯文件（executor._log 用，终端受开关控制）
    raw_file = open(path, 'a', encoding='utf-8', buffering=1)
    ts_file = TimestampedFile(raw_file)

    # TeeStderr（替换 sys.stderr，所有模块的 stderr 输出自动进文件+终端）
    tee = TeeStderr(sys.stderr, ts_file)

    raw_file.write(f'# Haku_EET 日志启动 {datetime.now().isoformat()}\n')
    raw_file.flush()

    return tee, ts_file
