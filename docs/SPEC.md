# Haku_EET 设计规格说明书

> 西门子杯竞赛电梯控制离散算法。Python + asyncio，本地 REPL 风格控制。
>
> 本文档是设计规格的真相来源（Source of Truth）。所有代码实现和用户文档（README / HANDOVER / COMMAND_MANUAL）均以此为准。

---

## 目录

1. [系统架构（大脑 / 小脑 / 脑干）](#1-系统架构大脑--小脑--脑干)
   - 1.1 [设计哲学：游戏化编程](#11-设计哲学游戏化编程)
   - 1.2 [总体结构](#12-总体结构)
   - 1.3 [核心设计原则](#13-核心设计原则)
   - 1.4 [什么是"不允许跳层"](#14-什么是不允许跳层)
2. [决策层——大脑](#2-决策层大脑)
   - 2.1 [用户交互模块](#21-用户交互模块)
   - 2.1.1 [PassengerQueue——大脑内独立的乘客请求队列](#211-passengerqueue大脑内独立的乘客请求队列)
   - 2.2 [算法层（调度员）](#22-算法层调度员)
   - 2.3 [模块边界](#23-模块边界)
3. [物理层——小脑](#3-物理层小脑)
   - 3.1 [executor——运动 FSM](#31-executor运动-fsm)
   - 3.2 [UI 模块——用户交互（灯/按钮/显示）](#32-ui-模块用户交互灯按钮显示)
   - 3.3 [Action 展开契约（小脑如何展开每个 Action）](#33-action-展开契约小脑如何展开每个-action)
   - 3.4 [硬件契约（赛前必验）](#34-硬件契约赛前必验)
4. [IO 抽象层——脑干](#4-io-抽象层脑干)
   - 4.1 [每部车独立的 IO 写通道](#41-每部车独立的-io-写通道)
   - 4.2 [tick 写合并——避免小包冲击 IO2HTTP](#42-tick-写合并--避免小包冲击-io2http)
5. [cron——外置耳机](#5-cron外置耳机)
   - 5.1 [核心概念](#51-核心概念)
   - 5.2 [两种事件规则](#52-两种事件规则)
   - 5.3 [关门场景完整序列](#53-关门场景完整序列)
   - 5.4 [关键设计约束](#54-关键设计约束)
   - 5.5 [数据结构](#55-数据结构)
   - 5.6 [run 循环](#56-run-循环)
6. [事件广播总线](#6-事件广播总线)
   - 6.1 [事件类型](#61-事件类型)
   - 6.2 [Listener 注册机制](#62-listener-注册机制)
   - 6.3 [端到端调用链（以"内召5楼"为例）](#63-端到端调用链以内召5楼为例)
7. [接口契约](#7-接口契约)
   - 7.1 [Action 枚举定义](#71-action-枚举定义)
   - 7.2 [Action 完成判据](#72-action-完成判据)
   - 7.3 [小脑状态机概览](#73-小脑状态机概览)
   - 7.4 [cron 接口](#74-cron-接口)
   - 7.5 [大脑内部模块间的数据接口](#75-大脑内部模块间的数据接口)
   - 7.6 [工程哲学例外](#76-工程哲学例外)
8. [模块文件结构](#8-模块文件结构)
   - 8.1 [层 <-> 模块映射总表](#81-层---模块映射总表)
9. [配置格式](#9-配置格式)
   - 9.1 [config/config.yaml](#91-configconfigyaml)
   - 9.2 [config/io_config.yaml](#92-configio_configyaml)
   - 9.3 [config/display_config.yaml](#93-configdisplay_configyaml)
10. [当前状态评估与未对齐项](#10-当前状态评估与未对齐项)
    - 10.1 [各层稳定性评估](#101-各层稳定性评估)
    - 10.2 [算法层未对齐项（需要升级）](#102-算法层未对齐项需要升级)
    - 10.3 [用户交互模块未对齐项（需要完善）](#103-用户交互模块未对齐项需要完善)
    - 10.4 [已验证 Critical 缺陷](#104-已验证-critical-缺陷)
11. [待办与路线图](#11-待办与路线图)
    - 11.1 [短期（比赛前必须修）](#111-短期比赛前必须修)
    - 11.2 [中期（对齐设计规格）](#112-中期对齐设计规格)
    - 11.3 [长期](#113-长期)
13. [代码嵌入的设计哲学](#13-代码嵌入的设计哲学)

> **交叉参考**：本 SPEC 是源按真相，同名主题在 HANDOVER.md §1 有更精简的入门表述，你设计代码时遵循的设计哲学就在该 SPEC/HANDOVER 的对应节里。

## 阅读指引

- 想理解 **为什么这么设计**？读 §1（设计哲学 + 三层架构）
- 想看 **每一层的入口/契约**？读 §2 / §3 / §4 / §5
- 想看 **事件如何流动**？读 §6
- 架构师设计 API / 改接口？读 §7
- 想看架构间映射与代码结构？读 §8
- 想调试 / 改 config？读 §9
- 想看 **还要补什么**？读 §10（未对齐项）与 §11（路线图）
- 想看 **代码里的具体设计哲学**？读 §13（代码嵌入的设计哲学）

---

## 1. 系统架构（大脑 / 小脑 / 脑干）

### 1.1 设计哲学：游戏化编程

**电梯是玩家，有属性有数值有状态。** 不要考虑物理世界，面向评测编程。

这是贯穿整个设计的核心思想。类比游戏引擎：

```
大脑（逻辑层）        修改游戏实体的属性值
  │
  ▼
小脑（物理层 = 运动 + 用户交互） 检测属性变化，同步到物理 IO
  │
  ▼
脑干（硬件层）        实际写入 PLC
```

游戏里你改角色的 `position` 属性，引擎自动把新位置呈现到屏幕上。
同理，Haku_EET 里大脑不会直接写 IO 地址——它改 `Car` 的属性，小脑自动把变化同步到物理世界。

这个模式带来的约束：
- **大脑只看 `Car`，不看 IO 地址**——`player.py` 是纯数据类
- **小脑负责双向同步**——IO 电平变化 → `Car` 属性更新，`Car` 属性变化 → IO 写入
- **脑干只做物理传输**——WS 收 / HTTP 发，不触碰任何电梯语义

### 1.2 总体结构

```
   ┌──────────────────────────────────────────────────────┐
   │               事件广播总线                              │
   │  每一层：广播自己的消息，监听别人的消息                   │
   └──────────────────────────────────────────────────────┘
          ▲                │
          │                ▼
   ┌──────┴────────────────────────────────────────────┐
   │              大脑（决策层）                           │
   │                                                    │
   │  ┌──────────────────┐  ┌──────────────────────┐   │
   │  │ 用户交互模块      │  │ 算法层（调度员）     │   │
   │  │ 赋予外设逻辑      │  │ 输入：全局状态+需求   │   │
   │  │ 翻译为需求事件    │  │ 输出：谁去哪         │   │
   │  │ 管理 cron 闹钟   │  │ 谁变更目的地         │   │
   │  │ 修改 Car 属性    │  │ 不碰外设语义         │   │
   │  └──────────────────┘  └──────────────────────┘   │
   │  ┌──────────────────┐                             │
   │  │ REPL 控制台       │                             │
   │  │ 文本命令 → 需求   │                             │
   │  └──────────────────┘                             │
   └──────────────────┬───────────────────────────────┘
                      │  修改 Car 属性 / 放入 ActionQueue
                      │
   ┌──────────────────┴───────────────────────────────┐
   │              小脑（物理层：运动 + 用户交互）           │
   │                                                    │
   │  ┌──────────────────┐  ┌──────────────────────┐   │
   │  │ executor FSM     │  │ UI 模块              │   │
   │  │ 运动控制         │  │ Car.ui ↔ IO 同步     │   │
   │  │ 更新 Car 位置/门 │  │ 7 段数码管显示       │   │
   │  └──────────────────┘  └──────────────────────┘   │
   │  ┌──────────────────┐                             │
   │  │ controllers      │                             │
   │  │ 电机/门硬件封装   │                             │
   │  └──────────────────┘                             │
   └──────────────────┬───────────────────────────────┘
                      │  IO 写入
   ┌──────────────────┴───────────────────────────────┐
   │           脑干（IO 抽象层）                          │
   │  把 IO 抽象成每个电梯的控制点位                     │
   │  WS bitmap 解析 → 信号名 / 写合并 tick → HTTP     │
   └──────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────┐
   │   cron（外置耳机）                                  │
   │   不属于任何层，只听事件广播                         │
   │   不"数秒"，而是定闹钟                              │
   │   EventRule：'reschedule' 推迟 / 'cancel' 自毁    │
   └──────────────────────────────────────────────────┘
```

### 1.3 核心设计原则

1. **三层严格分离**：大脑（决策层）/ 小脑（物理层：运动+用户交互）/ 脑干（IO 抽象层）通过极致抽象与解耦联系在一起，不允许跳层。
2. **电梯 = 玩家**：`Car` 是游戏实体，有属性有数值有状态，不掺杂 IO 地址。
3. **游戏属性驱动**：大脑修改 `Car` 属性，小脑自动同步到物理世界。
4. **算法层是调度员**：只输出"谁去哪/谁变更目的地"，不碰运动学实现。
5. **事件驱动**：每一层广播自己的消息、监听别人的消息。整个系统没有 `time.sleep`、没有轮询。
6. **cron 是外置耳机**：悬挂在事件总线上，不属于任何层，通过 EventRule 做 reschedule / cancel。
7. **不外设逻辑不污染调度**：用户交互模块只翻译需求，算法层只做调度决策。
8. **点位表 = IO 真相来源**：`gen_io_config.py` 解析 `点位表.md` 生成 `io_config.yaml`。

### 1.4 什么是"不允许跳层"

一个 IO 事件的生命路径是严格单向的：

```
IO电平变化
  → 脑干处理（信号名解析）→ 广播事件
    → 小脑 UI 模块 → 更新 Car 属性 → 广播属性变化
      → 大脑用户交互模块 → 翻译为需求事件 → 广播
        → 大脑算法层 → 调度 → Action → 入 ActionQueue
          → 小脑 executor → 展开 FSM → IO 操作
            → 脑干写入（HTTP POST）
```

每一层都有存在的理由。任何"跳过中间某层直接操作下层"的行为都是违反设计原则的。

---

## 2. 决策层（大脑）

大脑负责所有"与电梯运营相关的决策"。它只操作 `Car` 实体属性，不知道 IO 地址的存在。

### 2.1 用户交互模块

**职责**：赋予外设逻辑语义，翻译外设需求为电梯事件，修改 `Car` 属性。

外设包括但不限于：
- 物理 PLC 输入（按钮、光幕、限位、平层信号）
- REPL 文本命令
- 未来可能的外接模块

**做什么**：
- 把原始 IO 信号（`cabin_button_5=1`）翻译成电梯语义（"1号梯有人按了5楼"）
- 修改 `Car` 属性（`pending_calls.append(5)`、设置 `human_presence=1` 等）
- 把外设的时间维度行为通过 cron 管理（关门闹钟、熄灯闹钟）
- 广播需求事件给算法层

**不做什么**：
- 不做调度决策（不说"让1号梯去5楼"）
- 不直接操作小脑
- 不碰 IO 地址

**输入**：来自小脑的事件（`Car` 属性变化）、来自 cron 的事件（闹钟响）

**输出**：需求事件（内召/外召/开门/关门/变更目的地），通过事件总线广播

**当前代码映射**：
- `app.py` 中的 IO 事件处理（`_on_cabin_button`、`_on_hall_call_event` 等）
- `console.py`：REPL 命令处理
- `passenger.py`（可选）：乘客行为抽象

#### 2.1.1 PassengerQueue——大脑内独立的乘客请求队列

来源：`core/passenger.py:39-50`。

大脑拥有独立于小脑 `pending_calls` 的乘客请求队列 `PassengerQueue`。

**核心设计**：
- **三步工作流**：开门期间 `collect()` 缓存内召请求 → 关门后 `compile()` 生成 `_items` 路线 → `next()` 逐个消费 + `mark_served()` 标记完成
- **两种模式**：
  - `'discard'`（默认）：顺向未过站接受，已过站丢弃
  - `'keep'`：全部保留，到达当前目标后继续处理

**与算法层 `pending_calls` 的边界**：
- `PassengerQueue` 是大脑内部的数据结构，不污染算法层的 `pending_calls`
- 算法层只从 `pending_calls` 读当前需求，不直接访问 `PassengerQueue`
- 乘客流程管理通过 `PassengerQueue` 决定下一个去哪里，然后写入 `pending_calls` 供算法读取

### 2.2 算法层（调度员）

**职责**：在知道全局电梯在干什么的情况下，对用户交互模块广播的各种电梯需求事件做响应调度。

**做什么**：
- 接收全局电梯状态（所有 Car 的状态）
- 接收用户交互模块广播的需求事件
- 决定"哪部车去响应哪个需求"
- 输出 Action（谁去哪 / 谁变更目的地）
- 处理多车协调（群控）

**不做什么**：
- 不知道 IO 地址
- 不碰运动学实现（不知道什么是"反冲"、"刹车档位"）
- 不管理时间维度的行为

**输入**：全局电梯状态（所有 Car）+ 全局需求（pending_calls、hall_calls 等）

**输出**：`list[Action]`（每部车的动作列表），通过 ActionQueue 异步传递给小脑

**当前实现状态**：`SimpleInternalCall` 是对齐前的简化版本，只看到单部车、只响应内召。需要升级为真正的全局调度员。

### 2.3 模块边界

**关键约束**：用户交互模块不碰调度，算法层不碰外设语义。

```
用户交互模块说："1号梯有人按了5楼，这是一个需求"
算法层说："好的，1号梯当前空闲且在1楼，让它去5楼"
```

---

## 3. 物理层（小脑）

**职责**：**抽象电梯各种物理运行参数**（电机控制、接触器、刹车逻辑、反冲、平层、门控、灯光/按钮等），让"`/car 1 call 5`"这种高层命令成为可能——一句话就把电梯送到 5 楼，所有物理实现细节都被隐藏。

小脑负责双向同步——大脑修改的 `Car` 属性自动同步到物理世界，物理世界的变化也反映到 `Car` 属性上。下层用户（包括大脑）不需要知道任何电机、刹车、反冲的物理实现细节。

### 3.1 executor——运动 FSM

**做什么**：
- 从 ActionQueue 取 Action（由算法层放入）
- 把高层 Action 展开为具体的 IO 操作序列（拉接触器、继电器、刹车）
- 监听传感器信号确认动作完成
- 站点吸附（station_seek）：到站后自动监测平层，偏离时反冲修正
- 维护 `Car.position`、`Car.direction`、`Car.door_state` 等属性
- 广播完成事件

**不做什么**：
- 不做调度决策
- 不赋予外设逻辑语义

**输入**：Action（来自 ActionQueue）

**输出**：
- IO 操作（写入 IOClient）
- `Car` 属性更新
- 广播事件（动作完成 / 限位触达 / 平层信号）

**当前代码映射**：
- `executor.py`：FSM 核心
- `controllers.py`：MotorController、DoorController

**设计哲学清单**（完整阐述见 [§13 代码嵌入的设计哲学](#13-代码嵌入的设计哲学)）：

| 哲学 | 说明 | 参考 §13 |
|------|------|----------|
| `paused` 调试标志 | 手动调试模式完全旁路自动化保护，`on_io_event` 直接 return | D6 |
| `cache` 而非 `_last_*` 字段 | 多信号同步判定（如 level_up & level_down）必须读 cache，避免 dispatch 异步导致的字段更新顺序问题 | D2 |
| INITIALIZE 两段式 | 基站段全程低速防反冲冲过平层区，客运段复用标准减速 | D3 |
| EMERGENCY_STOP 同步清场 | 急停必须重置所有长寿命状态（_level_seek_active 等），防止旧逻辑复活撞限位 | D5 |
| NOOP 不退出保持模式 | 空动作不算新动作，不破坏站点吸附等长寿命状态 | D4 |
| `_arrive_and_brake` 统一刹车 | MOVE / INIT 路径共享到站逻辑，消除三份重复代码 | D7 |
| LIGHT_OFF / LIGHT_ON 保留 handler | 当前不 dispatch，预留未来 passenger_flow 模块扩展 | D8 |

### 3.2 UI 模块——用户交互（灯/按钮/显示）

**做什么**：
- 读取 `Car.ui` 属性（`full_load`、`fault`、`light`、`fan`、`cabin_button_leds`），同步写入物理 IO
- 把电梯状态（位置、方向、故障）同步到 7 段数码管和指示灯
- 大脑只管改属性，UI 模块负责同步到 IO

**不做什么**：
- 不决定什么时候亮什么灯（那是大脑用户交互模块的事）
- 不参与运动学控制

**当前代码映射**：
- `ui.py`：UiController，`Car.ui` 属性 ↔ IO 信号
- `display.py`：7 段数码管编码查表

**状态**：基本 stable。

**设计哲学**（完整阐述见 [§13 代码嵌入的设计哲学](#13-代码嵌入的设计哲学)）：

1. **UI 不是 PLC 影子**：`cabin_button_X` 按下不自动亮 LED——UI 控制器只负责何时亮，不负责按下按钮时自动亮。亮灯决策由上层逻辑（大脑用户交互模块）决定，为未来闪灯、复杂效果预留解耦。
2. **单一 IO 写路径**：所有 UI 写操作走 `set_many` 单一路径，由 IOClient tick 自动合并。每方法一次 `set_many`（后续可改成批量 flush，目前够用）。

### 3.3 Action 展开契约（小脑如何展开每个 Action）

小脑需要为每个 ActionKind 定义展开逻辑和完成判据。详见[第 7 节接口契约](#7-接口契约)。

### 3.4 硬件契约（赛前必验）

来源：`core/controllers.py:7-16`。

代码对真实 PLC 硬件存在若干假设，比赛现场必须逐条验证。

**已验证的假设**：
| 假设 | 默认值 | 若相反则修改 | 现场验证步骤 |
|------|--------|-------------|-------------|
| 电磁刹车接法：通电刹死 / 失电释放 | `brake_X = 0` 释放（默认常态），`brake_X = 1` 刹死 | 反转 `set_brakes()` 内部 0/1 映射 | 手动触发刹车信号，观察电梯是否按要求刹停或释放 |
| 限位开关常闭/常开 | 代码默认 2 限位触发即 `EMERGENCY_STOP` | 若常闭则需反转触发逻辑 | 触 1 限位观察行为是否触发减速而非急停 |
| 电机接触器极性与 PLC 输出匹配 | 正逻辑（1=吸合） | 改 `MotorController` 内部映射 | 手动逐个拉接触器，确认方向正确 |

> **现场验证要求**：上述每一条都是**代码假设**，不是实现缺陷。比赛现场需要安排专门的时间逐条验证，确认后再修改配置或代码映射。
>
> 验证步骤记录在 [HANDOVER.md §6.4](./HANDOVER.md#64-赛前硬件验证-checklist) 的扩展检查清单中。

---

## 4. IO 抽象层（脑干）

**职责**：把物理 IO 抽象成每个电梯的控制点位。

**做什么**：
- 对接下层物理设备（S7 PLC，通过 IO2HTTP 网关）
- WS bitmap 解析 → 信号名
- 写合并（accumulate 多个 output 变化 → tick flush）
- HTTP POST 写入 DB
- 广播 IO 电平变化事件

**不做什么**：
- 不赋予外设逻辑语义（不知道"cabin_button_5"是"去5楼"）
- 不参与任何调度或运动学决策

**输入**：来自 PLC 的 WS bitmap（上行），来自小脑的 IO 写入请求（下行）

**输出**：
- IO 事件（电平变化，广播给事件总线）
- HTTP POST（写给 PLC）

**当前代码映射**：
- `io_client.py`：WS + HTTP 双协议客户端，写合并 Buffer，tick flush
- `io_mapper.py`：DB 地址 <-> I 地址映射，信号名查找
- `virtual_plc.py`：模拟 PLC（simulate 模式下替代真实 PLC）

**状态**：基本 stable。

### 4.1 每部车独立的 IO 写通道

6 部车不共享同一个 IOClient 写实例。每部车有自己独立的 `io_write[cid]`，共享同一个 input/output cache（读统一走 `self.io`）。

原因：如果 6 部车共享一个 write buffer，一次 tick flush 出 30+ 个地址，S7 read-modify-write 顺序就是车号顺序，各车接触器实际建立时间会错开。

设计方案：`app.py:92-101`，`self.io_write: dict[int, IOClient]`。

### 4.2 tick 写合并 —— 避免小包冲击 IO2HTTP

**这是脑干最关键的设计之一：小脑调用 `io.set(addr, val)`，脑干不会立即 HTTP POST，而是把变化记到内部 buffer，等下一个 tick 周期（默认 20ms）一到，把这段时间内所有的变化合并成一次 HTTP POST。**

**为什么必须 tick？**

```python
# 不要这样写 —— 小脑毎个 set 都立即 HTTP POST
io.set('up_contactor', True)
io.set('motor_start', True)       # 一次往返十几毫秒
io.set('high_speed_contactor', True)  # 又一次往返
io.set('low_speed_contactor', True)   # 又一次往返

# 要这样写 —— set 只把意图记到 buffer
io.set('up_contactor', True)
io.set('motor_start', True)
io.set('high_speed_contactor', True)
io.set('low_speed_contactor', True)
# ... 20ms 后，tick flush 一次性 HTTP POST 一帧完整报文
```

**设计动机：**

- **小包冲击问题**：一次电机启动需要连续 4-5 个继电器 set。如果每个 set 都立即 HTTP 请求，IO2HTTP 在 1-2ms 内连续 4-5 次往返——不仅慢，还会与同 IO2HTTP 下别的服务争抢资源。
- **TCP 写合并天然支持**：多个小写入 TTL 一致时，内核会合并成一个大报文。但 HTTP POST 不会——每个 POST 都要重新建链或排队。
- **原子性保证**：tick flush 把一段时间内的所有变化打包成一帧 PLC 写入。S7 的 read-modify-write 是基于整帧的——分多次写可能导致中间状态被 PLC 读到（接触器建立顺序错乱）。

**工作机制：**

```
t=0ms    小脑 set up_contactor=True     → buffer 记录
t=2ms    小脑 set motor_start=True      → buffer 记录
t=5ms    小脑 set high_speed=True       → buffer 记录
t=8ms    小脑 set low_speed=True        → buffer 记录
t=20ms   tick 触发                       → 一次性 HTTP POST 整帧
t=21ms   S7 收到完整报文                 → 接触器按顺序建立
```

**参数：`tick_interval_ms`（默认 20ms = 50Hz）**

可通过 `config.yaml` 调整：

```yaml
io2http:
  tick_interval_ms: 20    # 写合并 tick 间隔，ms
  # 减小 → IO 延迟越低，但 HTTP 请求数越多
  # 增大 → HTTP 请求越少，但 接触器响应有可见延迟（电梯运动可见）
```

**取舍：**

- 20ms 延迟对电梯控制**完全可接受**——电机启动、刹车、门动作都是机械过程（几百毫秒级），20ms 延迟人眼不可察。
- 50Hz 频率（S7 PLC 标准的 IO 刷新率）也是 PLC 行业的工程惯例。
- **绝对不能在脑干做 `while 轮询`** ——轮询会让脑干失去"事件驱动"特性。tick 是一个独立的 asyncio 任务，`wait_for(tick_event, timeout=tick_interval_ms)`，事件或时间到就 flush。

**已知缺陷（来自 7/8 review）：**

- **idle 唤醒浪费**：当前即使没有 IO 变化，tick 也会每 20ms 触发一次 flush（空 flush）。已知性能缺陷 #5，详见 [§10.4](#104-已验证-critical-缺陷)。
- **重复写入浪费**：`set()` 即便值未变也写 buffer，触发下一 tick 的 HTTP POST。已知缺陷同上。

---

## 5. cron——外置耳机

**定位**：cron 是一个独立的事件驱动闹钟系统。它不属于任何层，而是作为一个独立组件悬挂在事件广播总线上。

**为什么不是 sleep**：

```python
# 不要这样写 —— 这是数秒，睡死了对外界无感知
await asyncio.sleep(10)
await close_door()

# 要这样写 —— 这是定闹钟，可被事件推迟/取消
cron.schedule(CronJob(
    trigger_time=time.monotonic() + 10,
    action=close_door,
    event_rules=[
        EventRule(signal_name='light_curtain', car_id=1,
                  action='reschedule', delay=10),
        EventRule(signal_name='cabin_button_open_door', car_id=1,
                  action='cancel'),
    ],
))
```

### 5.1 核心概念

```
CronJob（任务）
  ├─ trigger_time     闹钟响的绝对时间
  ├─ action           闹钟响时执行的回调
  ├─ auto_remove      自毁：触发后自动清除
  └─ event_rules      哪些事件影响这个闹钟

EventRule（事件规则）
  ├─ signal_name      IO 信号名
  ├─ car_id           哪部车
  ├─ action           'reschedule' | 'cancel'
  └─ delay            仅 reschedule：新延时
```

### 5.2 两种事件规则

| 规则 | 含义 | 例子 |
|------|------|------|
| **reschedule** | 事件触发时，把闹钟推到 `now + delay` | 关门闹钟：光幕一直被挡 → 无限推迟关门 |
| **cancel** | 事件触发时，直接销毁闹钟 | 熄灯闹钟：有人按了按钮 → 不熄灯 |

### 5.3 关门场景完整序列

```
1. 大脑用户交互模块收到"开门完成"事件 → 调度关门闹钟（10s）
2. cron 开始计时
3. 闹钟 pend 期间：
   a. 有人按开门按钮（cabin_button_open_door=1）
      → IO 抽象层广播 → 小脑更新 Car 属性 → 大脑用户交互模块收到
      → cancel 闹钟（别关了，人还要进）
   b. 按钮松开（cabin_button_open_door=0）
      → 大脑用户交互模块收到 → 调度新的关门闹钟（重新数 10s）
   c. 光幕被挡（light_curtain=1）
      → cron reschedule 闹钟（再推 10s）
4. 闹钟静悄悄走到了响：
   → cron 广播"闹钟响了"
   → 大脑用户交互模块监听到 → 翻译成"需求：关门" → 广播给算法层
   → 算法层决定：现在是否能关门？→ Action(CLOSE_DOOR)
   → 小脑 executor 展开关门动作
```

### 5.4 关键设计约束

- cron 是"聋子闹钟"——它不判断"该不该关"，只管理"什么时候响"
- "该不该"由大脑用户交互模块在收到事件广播后决定
- "怎么关"由算法层在收到需求后调度 Action
- "关的动作"由小脑 executor 展开

### 5.5 数据结构

```
_jobs:          name → CronJob           所有活跃任务
_heap:          [(trigger_time, name)]   小顶堆，最近要触发的排最前
_event_idx:     (car_id, sig) → [(job_name, action, delay)]
                                          IO 事件直达任务，O(1) 查询
_wakeup_event:  asyncio.Event            零轮询，只有事件才唤醒
```

### 5.6 run 循环

```
while running:
  fire 所有已过期的 job → 自毁 auto_remove
  wait_for(wakeup_event, timeout=next_deadline)
```

只在这三种情况下唤醒：
1. **定时到期**（`wait_for` timeout）→ fire job
2. **新任务 schedule**（`wakeup_event.set()`）→ 可能比当前等待更早
3. **IO 事件触发规则**（`_on_io_event` → `wakeup_event.set()`）→ 重调度/取消

---

## 6. 事件广播总线

所有层之间通过事件广播通信，不直接调用。事件总线不是一个独立的中间件——它是通过 Listener 模式在代码中实现的。

### 6.1 事件类型

| 事件类型 | 发出方 | 消费者 | 例子 |
|----------|--------|--------|------|
| IO 电平变化 | 脑干 | 小脑（UI）→ 大脑 | `(1, 'cabin_button_5', True)` |
| 需求事件 | 大脑（用户交互）| 大脑（算法层）| "1号梯需要去5楼" |
| Action | 大脑（算法层）→ 入 ActionQueue | 小脑（executor）| Action(MOVE_UP) |
| Action 完成 | 小脑（executor）| 大脑（用户交互）| `_on_action_done(Action(MOVE_UP))` |
| 闹钟响 | cron | 大脑（用户交互）| "10s 关门定时到了" |
| Car 属性变化 | 小脑 | 大脑 + 其他 | `Car.position=5` |
| 异常 / 急停 | 小脑（executor）| 所有层 | `EMERGENCY_STOP` |

### 6.2 Listener 注册机制

```python
# 小脑 UI 模块注册监听 IO 信号（把 IO 电平转为 Car 属性变化）
io.add_listener(
    signal_name='cabin_button_5',
    car_id=1,
    callback=ui_module._on_button,
)

# 大脑用户交互模块注册监听 Action 完成
executor.on_action_done = user_interaction._on_action_done
```

### 6.3 端到端调用链（以"内召5楼"为例）

```
步骤  │ 事件                              │ 处理层            │ 结果
──────┼───────────────────────────────────┼──────────────────┼────────────────
  1   │ cabin_button_5=1（PLC 输入变化）  │ 脑干              │ 广播 (1, 'cabin_button_5', True)
  2   │ IO 事件                           │ 小脑 UI → 大脑   │ Car.pending_calls += 5；广播需求
  3   │ 需求事件                          │ 大脑 算法层       │ decide() → Action(MOVE_UP)
  4   │ ActionQueue.put                   │ 大脑→小脑        │ executor 取到 Action
  5   │ MOVE_UP 展开                      │ 小脑 executor    │ 拉接触器 → 启电机 → 追平层
  6   │ 平层信号到达 5 楼                  │ 小脑 executor    │ 更新 Car.position=5；广播完成
  7   │ 动作完成事件                      │ 大脑 用户交互模块 │ 清 pending_calls → 决定下一步
```

全程没有 `await sleep()`、没有 `while: poll`、没有跨层硬调用。大脑不知道 IO，小脑不知道调度。

---

## 7. 接口契约

### 7.1 Action 枚举定义

来源：`core/actions.py:15-27`。

| ActionKind | 值 | 语义 | 参数 |
|------------|---|------|------|
| `INITIALIZE` | `"initialize"` | 启动定位 | 无 |
| `MOVE_UP` | `"move_up"` | 上行 | 目标楼层由 `car.target_floor` 决定 |
| `MOVE_DOWN` | `"move_down"` | 下行 | 目标楼层由 `car.target_floor` 决定 |
| `OPEN_DOOR` | `"open_door"` | 开门 | 无 |
| `CLOSE_DOOR` | `"close_door"` | 关门 | 无 |
| `SET_DISPLAY` | `"set_display"` | 设置 7 段数码管 | floor 或 glyph（二选一） |
| `EMERGENCY_STOP` | `"emergency_stop"` | 紧急停止 | 无 |
| `RESET_FAULT` | `"reset_fault"` | 复位故障 | 无 |
| `NOOP` | `"noop"` | 空动作（占位/心跳） | 无 |
| `LIGHT_OFF` | `"light_off"` | 熄灯（已弃用） | 无 |
| `LIGHT_ON` | `"light_on"` | 亮灯（已弃用） | 无 |

### 7.2 Action 完成判据

| Action | 什么算"完成" | timeout |
|--------|-------------|---------|
| `INITIALIZE` | 触 1 限位 → 反向逐层平层计数 → 停目标楼层 | 隐含在小脑逻辑中 |
| `MOVE_UP/MOVE_DOWN` | 平层信号匹配 `car.target_floor`、方向归零 | 站点吸附反冲可能额外几秒 |
| `OPEN_DOOR` | `door_open_done` 信号到位 | cron 兜底 8s (`door_complete_timeout`) |
| `CLOSE_DOOR` | `door_close_done` 信号到位 | cron 兜底 8s (`door_complete_timeout`) |
| `SET_DISPLAY` | IO 写入完成 | 无（同步写入） |
| `EMERGENCY_STOP` | 全部 output 归零 | 无（立即） |
| `RESET_FAULT` | FaultFlags 全清 | 无（立即） |
| `NOOP` | 立即 done | 无 |

### 7.3 小脑状态机概览

小脑内部是事件驱动的 FSM，每个 Action 对应一个子状态机。以下为主要状态迁移：

```
IDLE ──pop Action──> EXECUTING
  ├─ INITIALIZE        → INIT_SEGMENT → REVERSE → SETTLING → IDLE
  ├─ MOVE_UP/MOVE_DOWN → RUNNING → DECEL → LEVEL_SEEK → IDLE
  ├─ OPEN_DOOR         → OPENING → IDLE
  ├─ CLOSE_DOOR        → CLOSING → IDLE
  ├─ EMERGENCY_STOP    → 直接 -> IDLE（全输出归零）
  └─ NOOP/SET_DISPLAY  → 几乎瞬态

异常路径：
  EXECUTING 中触限位/急停 → EMERGENCY → IDLE
```

### 7.4 cron 接口

```python
# 调度一个闹钟
cron.schedule(CronJob(
    name='close_door_1',
    trigger_time=time.monotonic() + 10,
    action=close_door_callback,
    auto_remove=True,
    event_rules=[
        EventRule(signal_name='light_curtain', car_id=1,
                  action='reschedule', delay=10),
        EventRule(signal_name='cabin_button_open_door', car_id=1,
                  action='cancel'),
    ],
))

# 取消一个闹钟
cron.cancel('close_door_1')

# 停止所有闹钟（reset 时使用）
cron.stop()
```

### 7.5 大脑内部模块间的数据接口

**用户交互模块 → 算法层（需求投射）**：

```python
# 当前实现（简化版）
pending_calls: dict[int, list[int]]  # car_id → [目标楼层, ...]
hall_calls: dict[int, dict[str, bool]]  # floor → {up: bool, down: bool}

# 未来全局版的接口签名期望
class GlobalState:
    cars: dict[int, Car]
    pending_calls: dict[int, list[int]]
    hall_calls: dict[int, dict[str, bool]]
```

**算法层 → 小脑（Action 派遣）**：

```python
# 当前实现
action_queues: dict[int, ActionQueue]  # car_id → Queue[Action]
# 算法层通过 action_queues[cid].put(action) 派遣
```

### 7.6 工程哲学例外

**哲学**：事件驱动系统中不允许 `asyncio.sleep`——所有等 N 秒都应该用 cron + EventRule 实现。

**已知例外**：`executor.py:443-446` —— brake-before-stop 100ms wait。

```python
# 注：此 sleep 违反无 sleep/wait 哲学，但实测是 PLC 物理时序的必备
# dead time（详见 project/brake-before-stop.md）。不允许改成 cron 或删除，
# 除非实机复现过冲 bug 且有 PLC 反馈信号替换方案
await asyncio.sleep(0.1)
```

**为什么不能改为 cron**：
- `_arrive_and_brake` 调用 `hold_stop` 全刹 + 断电机后，需要 100ms 让机械刹车完全抱紧
- 若在 100ms 内调用 `_complete_action`，app 可能立刻发下一个 MOVE 动作，在刹车未完全释放时拉接触器，导致电机堵转过流
- cron 方式引入额外 sleep + callback 复杂度，且 100ms 固定延迟不需要响应事件——纯 sleep 更简单可靠

**替换方案 precondition**：
- 若未来有 PLC 反馈信号（霍尔传感器 / 刹车到位开关），可以删除此 sleep，改为 `wait_for(signal, timeout=...)`
- 在此之前：**不允许修改或删除**，除非在实机上复现过冲 bug 且有验证过的 PLC 替代方案

---

## 8. 模块文件结构

```
Haku_EET/
│
├── SPEC.md                  # 本文档——设计规格真相来源
├── HANDOVER.md              # 交接文档（基于 SPEC 的入门指南）
├── README.md                # 快速上手 + 目录结构
├── COMMAND_MANUAL.md        # REPL 命令手册 + 架构深读
├── IO_UI.md                 # 输出 IO <-> UI 模块映射
├── 点位表.md                 # IO 信号原始表（真相来源）
│
├── config/
│   ├── config.yaml          # 主配置
│   ├── io_config.yaml       # IO 映射（自动生成）
│   └── display_config.yaml  # 7 段数码管编码
│
├── tools/
│   └── gen_io_config.py     # 点位表 → io_config.yaml 解析脚本
│
├── core/
│   ├── actions.py           # Action 枚举 + ActionQueue（层间通信协议）
│   ├── player.py            # Car 游戏实体（电梯=玩家，属性/数值/状态）
│   ├── algorithm.py         # 大脑：算法层/调度员
│   ├── app.py               # 大脑：装配 + 用户交互事件处理 + 主协调
│   ├── console.py           # 大脑：REPL 控制台
│   ├── passenger.py         # 大脑：乘客行为抽象（可选）
│   ├── executor.py          # 小脑：运动 FSM
│   ├── controllers.py       # 小脑：电机/门控制
│   ├── ui.py                # 小脑：Car.ui 属性 ↔ IO 同步
│   ├── display.py           # 小脑：7 段数码管显示
│   ├── cron.py              # 外置：事件驱动闹钟
│   ├── io_client.py         # 脑干：WS + HTTP 客户端
│   ├── io_mapper.py         # 脑干：DB<->I 地址映射
│   ├── virtual_plc.py       # 脑干：模拟 PLC
│   ├── __init__.py
│   └── __main__.py          # CLI 入口
│
├── tests/
└── requirements.txt
```

### 8.1 层 <-> 模块映射总表

| 架构层 | 模块 | 职责 |
|--------|------|------|
| 大脑 | `app.py` | 装配 + 用户交互事件处理 + cron 协作 |
| 大脑 | `console.py` | REPL 文本命令 → 需求 |
| 大脑 | `passenger.py` | 乘客行为抽象（可选） |
| 大脑 | `algorithm.py` | 调度员：全局状态+需求 → Action |
| 大脑 | `actions.py` | Action 语言定义 + ActionQueue |
| 大脑 | `player.py` | Car 实体定义（属性/数值/状态） |
| 小脑 | `executor.py` | 运动 FSM：Action → IO 序列 |
| 小脑 | `controllers.py` | 电机控制、门控制 |
| 小脑 | `ui.py` | 灯/按钮/显示（用户交互）|
| 小脑 | `display.py` | 7 段数码管编码显示 |
| 脑干 | `io_client.py` | WS bitmap、HTTP 写合并 |
| 脑干 | `io_mapper.py` | DB <-> I 映射 |
| 脑干 | `virtual_plc.py` | 模拟 PLC |
| 外置 | `cron.py` | 事件驱动闹钟 |
| 工具 | `gen_io_config.py` | 点位表 → io_config.yaml |

---

## 9. 配置格式

### 9.1 `config/config.yaml`

```yaml
io2http:
  http_url: http://192.168.1.201:8080/gpio
  ws_url: ws://192.168.1.201:8081/
  tick_interval_ms: 20        # 写合并 tick 间隔，ms

building:
  min_floor: 1                # 正常使用最低层
  max_floor: 10               # 正常使用最高层
  top_base_floor: 11          # 上基站（只有限位没有门）
  bottom_base_floor: 0        # 下基站（只有限位没有门）

elevator:
  car_ids: [1,2,3,4,5,6]     # 启动时确定，不支持 /reload 增删
  initialization_direction: down   # 初始化方向（down / up）
  station_seek: false              # 站点吸附（默认关闭）
  door_complete_timeout: 8         # 门动作超时秒数
  door_close_delay: 10             # 关门延时（给 passenger_flow 用）
  light_off_delay: 600             # 熄灯延时（给 passenger_flow 用）

algorithm:
  name: simple_internal_call       # 首版只实现此算法

console:
  prompt: 'チルノ＄ '

logging:
  level: INFO
```

### 9.2 `config/io_config.yaml`

自动生成，由 `tools/gen_io_config.py` 解析 `点位表.md` 产生。结构：

```yaml
signals:
  cabin_button_5:
    car_1: { db_addr: DB10.DBX2.6, i_offset: 25 }
    car_2: { db_addr: DB10.DBX7.0, i_offset: 57 }
  # ...等所有信号
```

### 9.3 `config/display_config.yaml`

```yaml
glyphs:
  '0': [a,b,c,d,e,f]        # 7 段编码
  '1': [b,c]
  # ...
floor_display:
  10: 'A'                   # 10 楼显示字符映射
```

---

## 10. 当前状态评估与未对齐项

### 10.1 各层稳定性评估

| 层 | 状态 | 说明 |
|----|------|------|
| 脑干（IO 抽象层） | **STABLE** | WS 连接、bitmap 解析、写合并 tick flush、virtual_plc 均正常 |
| 小脑（物理层：运动+用户交互） | **STABLE** | "在 REPL 里面已经可以指哪停哪了"；UI 属性同步正常 |
| 大脑——用户交互模块 | **ALPHA** | IO 事件 → pending_calls 链条可用；passenger_flow 未实现；cron 交互链路待完善 |
| 大脑——算法层（调度员） | **ALPHA** | `SimpleInternalCall` 是对齐前的简化版本，未实现真正的全局调度 |

### 10.2 算法层未对齐项（需要升级）

| # | 问题 | 当前 | 目标 |
|---|------|------|------|
| 1 | 调度视野 | `decide(car, pending_calls)` 只看到一部车 | 输入全局电梯状态+全局需求 |
| 2 | 输出能力 | 只输出单部车的 Action | 输出"谁去哪 / 谁变更目的地" |
| 3 | 外召响应 | hall_call 点位已映射但算法层忽略 | 接收外召做分配决策 |
| 4 | 变更目的地 | 无 | 支持运行中变更目标 |
| 5 | 群控 | 无 | 多车协调 |

### 10.3 用户交互模块未对齐项（需要完善）

| # | 问题 | 说明 |
|---|------|------|
| 1 | passenger_flow 未实现 | 自动关门 cron、熄灯 cron、human_presence 三态迁移等 |
| 2 | cron 交互不完整 | 关门闹钟的挂起/恢复/延时监听窗口需用户交互模块实现 |
| 3 | hall_call 需求未翻译 | 外召信号已经映射了，但还没有翻译成需求事件广播给算法层 |

### 10.4 已验证 Critical 缺陷

来源：`.qwen/reviews/2026-07-08-153000-full-project.md`，详见该文档。

| # | 缺陷 | 所在层 | 位置 | 影响 |
|---|------|--------|------|------|
| 1 | `ActionQueue.get_nowait()` 缺失 | 通信协议 | `actions.py` | `/reset` 会崩溃 |
| 2 | `floor_display` 配置是死代码 | 小脑 | `display.py` + `executor.py` | 修改配置无效 |
| 3 | 全局信号误路由 | 大脑 | `app.py:246-251` | 当前安全，未来隐患 |
| 4 | 热路径冗余 mapper 查询 | 小脑 | `executor.py` | 性能浪费 |
| 5 | Bitmap 全扫描 + idle 唤醒 | 脑干 | `io_client.py` | idle 持续烧 CPU |
| 6 | 双重 MOVE dispatch | 大脑+小脑 | `app.py` + `executor.py` | 车可能冲过目标 |
| 7 | cron stop 后 listener 泄漏 | 外置 | `cron.py` | reload 后完全失效 |
| 8 | create_task 静默吞异常 | 全局 | 9+ 处 | 凌晨 oncall 噩梦 |
| 9 | WS reconnect 吞连接错误 | 脑干 | `io_client.py` | 宕机无告警 |
| 10 | 测试 18 个重复定义 | 测试 | `test_executor.py` | 覆盖假象 |

---

## 11. 待办与路线图

### 11.1 短期（比赛前必须修）

1. **修 Critical 缺陷**（见 §10.4）：
   - `ActionQueue.get_nowait()` 补上
   - `floor_display` 死代码修活
   - 全局信号路由修复
   - cron listener 泄漏修复
   - 异常 traceback 不吞
   - 重复测试整理
2. **补充安全关键测试覆盖**：
   - `_auto_seek_active`（auto-seek + 1-limit fallback + 2-limit emergency）
   - `_level_seek_check` 站点吸附反冲
   - `_on_close_event` 关门时光幕 breach → 反开
   - `_emergency_stop_flag` race fix 路径
   - `_start_close_door_cron_job` 光幕仍在则 reschedule

### 11.2 中期（对齐设计规格）

1. **升级算法层**：从 `SimpleInternalCall` 到真正的全局调度员
   - 输入扩展为全局状态 + 全局需求
   - 支持外召分配
   - 支持变更目的地
2. **完善用户交互模块**：
   - 实现 passenger_flow（关门 cron、熄灯 cron、human_presence 三态迁移）
   - 用户交互模块与 cron 的完整交互（关门闹钟挂起/恢复/延时窗口）
   - hall_call 需求翻译
3. **性能优化**（review Suggestion）：
   - `_all_outputs_off` 线性查表优化
   - `display._write_segments` 每段一次 mapper 查优化
   - 6 部车 sequential 初始化 → `asyncio.gather`

### 11.3 长期

1. 多算法热切换（`/algo set <name>`）
2. 群控调度算法
3. Web 控制台
4. 完整集成测试环境

---

## 13. 代码嵌入的设计哲学

本文档不同于 §1.3 和 §3.1 等章节中列出的高层设计原则——本节收集的是**直接在源代码中以注释形式沉淀的设计思想**。每条都附有原始来源和行号，方便在重构或修改时查阅。

### 13.1 A——大脑不注册 IO 监听器（passenger.py）

来源：`core/passenger.py:1-22`。

| # | 思想 | 应用场景 |
|---|------|----------|
| A1 | **大脑不注册 IO listener**：大脑不注册任何 IO 监听器，不接触任何 IO 事件。外召/内召/门按钮/光幕等原始 IO 事件由小脑 app.py 处理，处理完后再调用大脑的流程管理方法。 | 添加新 IO 信号时：不要在大脑模块中直接注册 listener，始终通过 app.py 转发 |
| A2 | **PassengerQueue——大脑内独立队列**：拥有独立的乘客请求队列，不污染算法层的 `pending_calls`。 | 扩展乘客流程时：修改 PassengerQueue 而非 pending_calls |
| A3 | **三步工作流 + 两种模式**：collect（开门期间 cache）→ compile（关门后生成路线）→ consume（next + mark_served）。两种模式：discard（顺向未过站接受，已过站丢弃）/ keep（全部保留）。 | 修改乘客流程算法时参考 |

### 13.2 B——UI 不自动绑定事件（ui.py）

来源：`core/ui.py:12-14`。

| # | 思想 | 应用场景 |
|---|------|----------|
| B1 | **UI 不是 PLC 影子**：UI 控制器只负责何时亮，不负责按下按钮时自动亮——上层逻辑（大脑用户交互模块）决定。即使现在看上去按下就亮是合理的，也不绑定。 | 新增 UI 元素时：不要在 UI 模块中自动绑定按钮 ↔ LED 逻辑 |
| B2 | **单一 IO 写路径**：所有 UI 写操作走 `set_many` 单一路径，由 IOClient tick 自动合并。 | 添加新的 UI 输出时：复用 `set_many`，不要引入独立的 HTTP 写入路径 |

### 13.3 C——PLC 硬件契约（controllers.py）

来源：`core/controllers.py:7-16`。

| # | 思想 | 应用场景 |
|---|------|----------|
| C1 | **硬件接法假设**：电磁刹车——通电刹死 / 失电释放。所以 `brake_X = 0` = 释放（默认常态），`brake_X = 1` = 刹死。 | 比赛现场验证：如果 PLC 接法相反，修改 `set_brakes` 映射 |
| C2 | **可逆性**：若现场硬件接法相反，改一处即可（修改 `set_brakes` 内部映射，不改 executor / UI）。 | 硬件适配：确保修改集中在 controllers.py |
| C3 | **验证步骤**：比赛现场需要单独验证刹车极性。 | 赛前 checklist：见 HANDOVER.md §6.4 |

### 13.4 D——executor 设计哲学（executor.py）

来源：`core/executor.py`。

| # | 思想 | 位置 | 应用场景 |
|---|------|------|----------|
| D1 | **哲学例外：brake-before-stop 100ms wait**：此 sleep 违反零 sleep 哲学，但实测是 PLC 物理时序的必备 dead time。不允许改成 cron 或删除，除非有 PLC 反馈信号。 | `executor.py:443-446` | 修改刹车流程时：保留此 sleep，不要试图删除或 cron 化 |
| D2 | **cache 而非 `_last_*` 字段**：多信号同步判定（如 level_up & level_down 同时为 1）必须读 cache，因为 dispatch 异步导致 `_last_*` 字段更新有先后。单信号边沿检测可用字段。 | `executor.py:288-292` | 添加新传感器信号判定时：多信号同时判定用 cache，单信号边沿检测用字段 |
| D3 | **INITIALIZE 两段式**：基站段全程低速（防反冲第一层高速冲过平层区刹不住），客运段复用标准减速（≥2 层高速，=1 层低速）。 | `executor.py:_apply_init_decel` | 修改初始化逻辑时：保持基-客分段设计 |
| D4 | **NOOP 不退出保持模式**：空动作不算新动作，不破坏长寿命保持状态（如站点吸附）。算法在空闲时持续发 NOOP，若每次退出 hold 会让吸附永久不激活。 | `executor.py:598-604` | 设计新 ActionKind 时：明确定义什么算新动作 |
| D5 | **EMERGENCY_STOP 同步清场**：急停必须重置所有长寿命状态（`_level_seek_active`、`_relevel_future`、`_auto_seek_active` 等），否则后续事件会复活旧逻辑导致撞限位。 | `executor.py:398-409` | 添加新的长寿命状态时：必须在 `_emergency_stop` 中同步清场 |
| D6 | **paused 调试标志**：手动模式下设为 True，`on_io_event` 直接 return 不做任何处理，让手动调试模式完全 raw（限位、状态机、IO 写都不会干扰）。 | `executor.py:88-90` | 手动调试功能维护时：保持 paused 的完全旁路语义 |
| D7 | **`_arrive_and_brake` 统一刹车流程**：MOVE 和 INIT 路径共用到站逻辑（全刹→方向归零→100ms 固位→激活站点吸附→完成动作），消除三处重复代码。 | `executor.py:428-452` | 修改到站行为时：不要复制 _arrive_and_brake，扩展它 |
| D8 | **LIGHT_OFF / LIGHT_ON 保留 handler**：当前不被 app 控制层 dispatch，保留 handler 是为未来 passenger_flow 模块预留的扩展点。 | `executor.py:685-698` | 实现 passenger_flow 时：启用 dispatch 路径 |
| D9 | **`_emergency_stop_flag` race condition 防护**：急停后所有前序 await 必须 panic，不能让 stale 任务继续标记完成。 | `executor.py:129-130` | 添加新的 await 操作时：确保急停后不会继续完成 |
