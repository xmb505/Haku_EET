---
kind: dependency_management
name: Python 依赖管理（requirements.txt + 标准库）
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
---

本项目为纯 Python 异步电梯控制核心，依赖管理采用最简方案：根目录 `requirements.txt` 声明所有第三方包，无 lockfile、无 vendoring、无私有源配置。

- **包清单**（`requirements.txt`）
  - `aiohttp>=3.9`：HTTP 客户端/服务器
  - `websockets>=12.0`：WebSocket 通信
  - `prompt-toolkit>=3.0`：交互式控制台
  - `PyYAML>=6.0`：配置文件解析（被 `core/app.py`、`core/display.py`、`core/io_mapper.py`、`core/passenger.py` 等多处使用）
  - `pytest>=7.4`、`pytest-asyncio>=0.23`：测试框架与异步支持

- **约定与约束**
  - 仅使用 `>=` 宽松版本约束，未锁定精确版本，也不存在 `requirements.in` / `pip-tools` / `poetry` / `pipenv` 等工具链。
  - 全部第三方依赖均为 PyPI 公开包，未见 `pip.conf`、`setup.cfg`、`pyproject.toml` 或任何私有仓库地址配置。
  - 除上述 5 个第三方包外，其余导入均来自 Python 标准库（`asyncio`、`dataclasses`、`enum`、`pathlib`、`typing`、`argparse`、`sys`、`time` 等），无需额外安装。

- **缺失项**
  - 无 `requirements-dev.txt`、`constraints.txt`、`Pipfile.lock`、`poetry.lock` 等同步锁文件，无法保证构建可重现性。
  - 无 CI 中固定版本的策略，升级时可能引入不兼容变更。