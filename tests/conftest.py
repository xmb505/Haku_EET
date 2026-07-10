# 让 pytest 能找到 core 包
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'config.yaml'
IO_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'io_config.yaml'
DISPLAY_PATH = Path(__file__).resolve().parent.parent / 'config' / 'display_config.yaml'


@pytest.fixture
async def app_and_console():
    """simulate 模式 app + console fixture

    供 test_app.py / test_buttonui_batch.py / 等需要直接调 cmd_xxx 的测试使用。
    """
    # 延迟 import 避免 conftest 加载阶段崩
    from core.app import App
    from core.console import Console

    a = App(
        config_path=CONFIG_PATH,
        io_config_path=IO_CONFIG_PATH,
        display_config_path=DISPLAY_PATH,
        simulate=True,
    )
    await a.start()
    c = Console(a)
    yield a, c
    await a.stop()