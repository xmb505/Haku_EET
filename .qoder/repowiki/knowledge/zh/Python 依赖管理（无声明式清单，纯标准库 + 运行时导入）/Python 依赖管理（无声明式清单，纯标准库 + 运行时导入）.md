---
kind: dependency_management
name: Python 依赖管理（无声明式清单，纯标准库 + 运行时导入）
category: dependency_management
scope:
    - '**'
---

本仓库为 Python 项目，但**未使用任何声明式依赖管理机制**：根目录与子目录下不存在 `requirements.txt`、`pyproject.toml`、`setup.py`、`Pipfile`、`poetry.lock`、`tox.ini`、`Makefile`、`Dockerfile` 等任何依赖清单或构建脚本。代码通过裸 `import` 引入第三方包，且仅使用了以下外部依赖：
- `aiohttp`（HTTP 客户端，用于 IOClient 向 PLC 发送 POST 请求）
- `websockets`（WebSocket 客户端，用于订阅 PLC 输入事件）
- `PyYAML`（`yaml` 模块，用于加载 `config/*.yaml` 配置文件）

所有其他导入均为 Python 标准库（`asyncio`、`argparse`、`dataclasses`、`enum`、`pathlib`、`typing`、`time`、`sys`）。由于缺少依赖清单文件，无法锁定版本、无法在 CI 中自动安装、也无法进行安全扫描或升级审计。当前运行环境完全依赖开发者本地已安装的包集合，存在“在我机器上能跑”的风险。

此外，仓库未配置私有 PyPI 源、未使用 vendoring、也未见任何虚拟环境或容器化约束，属于最原始的“裸 import”模式。