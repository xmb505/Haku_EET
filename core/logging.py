"""
logging.py —— 日志文件 + stderr 分流

策略：
- sys.stderr 整体替换为 TeeStderr → 所有 stderr 输出自动写文件 + 终端
- executor._log 额外走纯文件（文件始终写），终端受 exec_log_enabled 控制
"""

import sys
import os
from datetime import datetime


class TeeStderr:
    """同时写入原始 stderr 和日志文件的 stream 包装器"""
    def __init__(self, original_stderr, log_file_path: str) -> None:
        self._stderr = original_stderr
        self._file = open(log_file_path, 'a', encoding='utf-8', buffering=1)
        self._file_path = log_file_path

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
        self._file.close()

    @property
    def path(self) -> str:
        return self._file_path


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
    file_stream = open(path, 'a', encoding='utf-8', buffering=1)

    # TeeStderr（替换 sys.stderr，所有模块的 stderr 输出自动进文件）
    tee = TeeStderr(sys.stderr, path)

    file_stream.write(f'# Haku_EET 日志启动 {datetime.now().isoformat()}\n')
    file_stream.flush()

    return tee, file_stream
