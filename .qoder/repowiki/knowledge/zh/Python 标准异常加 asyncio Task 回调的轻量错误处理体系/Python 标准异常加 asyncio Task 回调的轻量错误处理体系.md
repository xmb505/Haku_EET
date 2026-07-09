---
kind: error_handling
name: Python 标准异常加 asyncio Task 回调的轻量错误处理体系
category: error_handling
scope:
    - '**'
source_files:
    - core/app.py
    - core/__main__.py
    - core/io_client.py
    - core/io_mapper.py
    - core/console.py
    - core/display.py
    - core/passenger.py
    - tools/gen_io_config.py
---

本仓库未定义统一的自定义异常类型或全局错误码，而是采用 Python 标准异常（ValueError / KeyError / IOError / RuntimeError）配合 asyncio 后台任务异常日志回调的轻量模式。

1. 异常类型约定
- 参数/配置校验：统一 raise ValueError(...)，消息包含具体非法值与期望范围，如 core/app.py 中楼层越界、方向非法；core/console.py、core/display.py、core/passenger.py 均遵循此约定。
- 配置映射缺失：IOMapper 对未知信号 raise KeyError(...)，调用方用 try/except KeyError 做降级处理（例如 set_usermode 中 ready 信号不存在时 pass）。
- I/O 层失败：IOClient.flush 失败 raise IOError(...)，模拟模式误用 raise RuntimeError(...)
- 工具脚本：tools/gen_io_config.py 使用 RuntimeError 报告点位表格式异常。

2. 异步异常传播与兜底
- 所有通过 _fire_and_forget 创建的后台 task 都注册了 add_done_callback(_on_done)，在 task 完成时打印 [app] 后台 task... 异常: ...，防止 create_task 吞掉异常。
- 高层回调（如 PassengerManager.on_action_done）被 try/except Exception 包裹并 print 警告，确保上层插件异常不中断小脑主循环。
- 门动作控制 (control_door) 将“派发成功但后续出错”的场景拆成：立即返回 {status:'dispatched'} + 后台 _door_track_completion task 跟踪完成/错层 + cron 定时兜底释放 mutex，避免 REPL 阻塞且保证资源最终释放。

3. 顶层入口保护
- core/__main__.py 的 main() 仅捕获 KeyboardInterrupt 做优雅退出，其余异常由 asyncio.run 默认行为处理，无全局 try/except 吞异常。
- core/app.py 启动阶段对可选依赖 PassengerManager 使用 try/except ImportError 做可选加载，缺失时降级为 None。

4. 设计决策与约束
- 没有集中式 error_handling 模块或自定义基类，错误以“快速失败 + 明确消息”的方式在调用点抛出，由上层决定是消费还是继续冒泡。
- 对外暴露的 API（App 方法）优先返回结构化 dict（如 control_door、change_internal、fireman），内部校验失败直接 return rejected 状态，而非抛异常，使 REPL 交互稳定。
- 对不可恢复的底层错误（HTTP flush 失败、地址越界等）仍抛异常，交由 _fire_and_forget 回调记录，不向上冒泡到主循环。

开发者应遵循的规则
- 参数/配置校验一律 raise ValueError，消息写明“期望 vs 实际”。
- 查找映射失败 raise KeyError，调用方用 try/except KeyError 做降级，不要吞掉其他异常。
- 需要后台执行的操作必须通过 _fire_and_forget 创建 task，禁止裸 create_task。
- 对外 API 若可预期失败，返回 dict 状态码；仅在真正异常路径抛异常。
- 上层插件回调（PassengerManager 等）必须 try/except Exception 包裹并 print 警告，不得让异常泄漏到小脑主循环。