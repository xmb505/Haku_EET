---
kind: logging_system
name: 日志系统 — 基于 print 的简易控制台输出
category: logging_system
scope:
    - '**'
source_files:
    - core/app.py
    - core/console.py
    - core/__main__.py
---

本仓库未引入任何 Python logging 框架（如 `logging`、`loguru`、`structlog` 等），也未发现独立的日志模块或配置文件。所有“日志”输出均通过内置 `print()` 函数直接打印到标准输出，属于最基础的调试/交互输出方式。

**现状与模式**
- 核心运行日志集中在 `core/app.py`，使用带前缀标签的 f-string 格式，例如 `[app]`、`[tick]`、`[vplc]`、`[emergency]`、`[reload]`、`[clear]`、`[car {id}]` 等，用于区分不同子系统。
- REPL 交互界面位于 `core/console.py`，大量 `print()` 用于展示帮助信息、命令回显、状态快照和错误提示。
- 入口文件 `core/__main__.py` 仅用 `print` 输出一句告别语。
- 未发现任何日志级别控制、日志轮转、结构化字段、文件/网络 sink 或异步安全写入机制。

**结论**：该仓库不存在成型的日志系统，当前仅依赖 `print()` 进行控制台输出，不具备生产级可观测性能力。若需引入结构化日志，建议统一封装 logger 并集中配置级别与输出目标。
