---
kind: frontend_style
name: 电梯 UI 指示灯与交互样式体系
category: frontend_style
scope:
    - '**'
source_files:
    - core/ui.py
    - config/ui_config.yaml
    - IO_UI.md
---

本仓库的"前端样式"并非传统 Web 前端，而是面向西门子 PLC 硬件的物理 UI 指示灯与数码管显示系统。其视觉表现由 Python 代码通过 IO 映射直接驱动硬件 LED、7 段数码管和外召灯，不存在 CSS/SCSS/Tailwind 等浏览器样式技术。

## 1. 采用的体系与方法
- UI 控制器模式：core/ui.py 中的 UiController 类封装所有 UI 相关 IO 写操作（满载/故障/照明/风扇/开门指示/轿内按钮灯），上层仅通过 set_xxx(bool) 方法修改状态，禁止直接赋值 car.ui.fault = True（那只会改逻辑状态不同步 IO）。
- 配置驱动外观行为：config/ui_config.yaml 定义外召指示灯闪烁间隔、关门延时、熄灯节能延时、队列模式等运行时视觉参数，支持 /reload 热重载。
- IO 地址映射解耦：通过 IOMapper 将逻辑信号名（如 fault_indicator）映射到具体 DB 地址（如 DBX5.4），使 UI 逻辑与硬件布局完全分离。
- 批量同步机制：提供 sync_to_io() 一次性把 Car.ui 全量状态写入 IO，避免多次 tick 造成的闪烁。

## 2. 核心文件与包
- core/ui.py — UI 控制器实现，定义 set_full_load/set_fault/set_light/set_fan/set_cabin_button_led/sync_to_io 等方法
- config/ui_config.yaml — UI 行为参数（闪烁间隔、延时、队列模式）
- IO_UI.md — 完整的输出 DB11 点位表与 UI 方法→IO 信号映射文档
- core/io_mapper.py — 负责逻辑信号名到 DB 地址的映射解析

## 3. 架构约定与设计决策
- UI 属于小脑（物理层）：只负责"看上去是什么样的"，不决定"什么时候亮什么灯"——后者由大脑（乘客调度）或算法层决策。
- 单向依赖：UI 模块依赖 IOClient/IOMapper 写入硬件，但被上层（app/controllers/passenger）调用，不反向依赖业务逻辑。
- 无自动事件绑定：轿内按钮按下不会自动亮 LED，上层逻辑自行决定是否点亮（为未来闪灯/复杂效果预留解耦空间）。
- 每车独立 IO 写通道：每个 UiController 持有独立的 IOClient 实例，避免 6 部梯共享写通道拥堵。

## 4. 开发者应遵循的规则
- 必须通过 app.ui[cid].set_fault(True) 等 API 修改 UI 状态，严禁直接赋值 car.ui.fault = True。
- 新增 UI 信号时，需在 ui_config.yaml 中声明对应 IO 名称，并在 io_config.yaml 中完成 DB 地址映射。
- 批量更新场景使用 sync_to_io() 而非逐位调用 set()，减少 IO 往返。
- 外召指示灯闪烁频率通过 flash_interval_ms 配置，不要硬编码在代码中。
- 开关门继电器 (door_open_relay/door_close_relay) 不属于 UI 模块管辖，由 DoorController 控制。