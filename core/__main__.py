"""
启动 Haku_EET 主程序

用法:
    python3 -m core                           # 连真实 IO2HTTP（默认）
    python3 -m core --simulate                # 纯软件模拟（无硬件调试）
    python3 -m core --config <path>           # 指定 config.yaml
    python3 -m core --io-config <path>        # 指定 io_config.yaml
    python3 -m core --display-config <path>   # 指定 display_config.yaml
"""

import argparse
import asyncio
import sys
from pathlib import Path

from .app import App
from .console import Console


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='python3 -m core',
        description='Haku_EET —— 西门子杯电梯控制离散算法',
    )
    parser.add_argument(
        '--simulate', action='store_true',
        help='纯软件模拟模式（不连真实 IO2HTTP）',
    )
    parser.add_argument(
        '--config', type=Path,
        default=PROJECT_ROOT / 'config' / 'config.yaml',
        help='主配置路径',
    )
    parser.add_argument(
        '--io-config', type=Path,
        default=PROJECT_ROOT / 'config' / 'io_config.yaml',
        help='IO 映射配置路径',
    )
    parser.add_argument(
        '--display-config', type=Path,
        default=PROJECT_ROOT / 'config' / 'display_config.yaml',
        help='7 段数码管配置路径',
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    app = App(
        config_path=args.config,
        io_config_path=args.io_config,
        display_config_path=args.display_config,
        simulate=args.simulate,
    )
    console = Console(app)

    try:
        await app.start()
        await console.run()
    finally:
        await app.stop()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('\n再见')
        sys.exit(0)


if __name__ == '__main__':
    main()