---
kind: build_system
name: 构建系统：无自动化构建脚本的纯 Python 项目
category: build_system
scope:
    - '**'
---

经对仓库根目录及核心模块扫描，未发现任何与构建、打包、测试或部署相关的文件（如 Makefile、Dockerfile、setup.py、pyproject.toml、requirements.txt、tox.ini、pytest.ini、.github/workflows、build.sh 等）。该项目是一个以 core/__main__.py 为入口的纯 Python 应用，依赖通过 Python 解释器直接运行，未引入任何第三方构建工具或 CI/CD 流水线。因此本仓库不存在可归纳的 build_system 体系。