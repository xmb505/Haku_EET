---
kind: logging_system
name: 日志系统：基于 print 的简易控制台输出，无结构化日志框架
category: logging_system
scope:
    - '**'
source_files:
    - core/__main__.py
    - core/app.py
    - core/console.py
---

本仓库未引入任何 Python 标准库 logging、loguru、structlog 等日志框架，也未发现 log/ 或 logging/ 目录。全项目运行时输出全部通过内置 `print()` 直接写入 stdout，属于最基础的“裸打印”模式。

**现状与证据**
- 核心模块 `core/app.py`、`core/console.py`、`core/__main__.py` 中大量使用 `print(f'[tag] ...')` 形式的调试输出，标签如 `[vplc]`、`[tick]`、`[app]`、`[emergency]`、`[reload]`、`[clear]`、`[car {id}]` 等仅作为前缀区分来源，并非真正的日志级别。
- 所有输出均为纯文本字符串拼接，不包含时间戳、线程/协程标识、结构化字段（JSON），也没有文件 sink 或轮转策略。
- `requirements.txt` 中未声明任何第三方日志依赖；`__main__.py` 启动流程也不进行任何 logger 初始化。

**影响与建议**
- 当前模式适合本地交互式调试（REPL + 终端直读），但无法在无人值守运行、故障回溯、多进程/异步并发场景下可靠采集信息。
- 若后续需要升级，建议引入 `logging` 标准库或 `loguru`，集中配置 formatter（包含时间、level、logger name、coroutine id）、按模块划分 logger、并支持同时输出到 stdout 与文件。