---
kind: build_system
name: Python 包与构建体系（无 Makefile/Docker，纯 pip + pytest）
category: build_system
scope:
    - '**'
source_files:
    - requirements.txt
    - pytest.ini
    - core/__main__.py
    - tools/gen_io_config.py
    - .gitignore
---

本项目是一个基于 asyncio 的电梯控制核心 Python 应用，未使用任何传统构建系统（无 Makefile、Dockerfile、setup.py/pyproject.toml、CI 流水线）。其构建完全围绕 Python 标准生态展开：

1. 依赖管理 — 仅一个 requirements.txt，声明运行时与测试依赖（aiohttp、websockets、prompt-toolkit、PyYAML、pytest、pytest-asyncio），通过 pip install -r requirements.txt 安装。
2. 入口与运行 — 以 python3 -m core 作为唯一启动方式，由 core/__main__.py 解析命令行参数（--simulate、--config、--io-config、--display-config）并启动事件循环；工具脚本 tools/gen_io_config.py 提供可执行 shebang，用于从 点位表.md 生成 config/io_config.yaml。
3. 测试 — 使用 pytest + pytest-asyncio，pytest.ini 将 tests/ 设为 testpaths 并开启 asyncio_mode = auto，测试文件遵循 test_*.py 命名约定。
4. 配置驱动 — 所有硬件映射、楼层、UI 等外部化到 config/*.yaml，由 gen_io_config.py 从文档自动生成，避免硬编码。
5. 打包/发布 — 仓库中不存在任何打包产物或发布流程（无 dist/、no wheel、no Docker image），部署方式为直接拷贝源码并在目标环境执行 python3 -m core。

开发者应遵循的约定：
- 新增依赖只改 requirements.txt，不要引入其他依赖管理工具。
- 新增模块需在 core/ 下并通过 python3 -m core 可被主程序发现。
- 修改 点位表.md 后必须重跑 python3 tools/gen_io_config.py 更新 config/io_config.yaml。
- 新增测试放在 tests/test_xxx.py，使用 @pytest.mark.asyncio 或依赖 auto 模式编写异步用例。