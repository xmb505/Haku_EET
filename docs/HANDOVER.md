# Haku_EET 交接文档

西门子杯竞赛电梯控制离散算法。Python + asyncio，本地 REPL 风格控制。

这份文档是**总纲**——新成员 / 接班 AI 第一份要读的东西。读完它再看 README（命令与目录）和 COMMAND_MANUAL（命令手册）。

设计规格真相来源：[SPEC.md](./SPEC.md) | 命令速查：[COMMAND_MANUAL.md](./COMMAND_MANUAL.md) | 点位表：[IO_UI.md](./IO_UI.md)

## 目录

- [0. 30 秒上手](#0-30-秒上手)
- [1. 系统架构（大脑 / 小脑 / 脑干）](#1-系统架构大脑--小脑--脑干)
  - [1.1 设计哲学：游戏化编程](#11-设计哲学游戏化编程)
  - [1.2 总体结构](#12-总体结构)
  - [1.3 大脑（决策层）](#13-大脑决策层)
  - [1.4 小脑（物理层：运动 + 用户交互）](#14-小脑物理层运动--用户交互)
  - [1.5 脑干（IO 抽象层）](#15-脑干io-抽象层)
  - [1.6 cron——外置耳机](#16-cron外置耳机)
- [2. 设计哲学的 8 条不变量](#2-设计哲学的-8-条不变量)
- [3. 模块导航（代码 <-> 架构映射）](#3-模块导航代码---架构映射)
  - [3.1 按架构层映射](#31-按架构层映射)
  - [3.2 .qwen/skills 导航（反向索引）](#32-qwenskills-导航反向索引)
- [4. Action 契约表](#4-action-契约表)
- [5. 端到端调用链（事件驱动视角）](#5-端到端调用链事件驱动视角)
- [6. 已知雷区](#6-已知雷区)
- [7. 业务约束](#7-业务约束)
- [8. 进一步阅读](#8-进一步阅读)

---

## 0. 30 秒上手

```bash
# 模拟模式（不需要硬件）
pip install -r requirements.txt
python3 tools/gen_io_config.py       # 首次/点位表变更后
python3 -m core --simulate

# 进入 REPL 后
haku> /car 1 init down               # 初始化
haku> /car 1 call 5                  # 内召 5 楼
haku> /car 1 status                  # 看状态
haku> /quit                          # 退出
```

关键不变量（读到下文详细解释时心里始终绷着这根弦）：

1. 三层分离：大脑（决策）/ 小脑（物理层：运动+用户交互）/ 脑干（IO 抽象），每一层靠事件广播联系
2. 电梯 = 玩家——Car 是游戏实体，有属性有数值有状态，不碰 IO 地址
3. 算法层是调度员——只输出"谁去哪"，不管"怎么去"
4. 事件驱动——每一层都广播自己的消息、监听别人的消息
5. cron 是外置耳机——不是数秒，是定闹钟 + EventRule
6. 不外设逻辑不污染调度——用户交互模块只翻译需求，算法层只做调度
7. 点位表 = IO 真相来源

---

## 1. 系统架构（大脑 / 小脑 / 脑干）

### 1.1 设计哲学：游戏化编程

**电梯是玩家，有属性有数值有状态。** 不要考虑物理世界，面向评测编程。

这是贯穿整个设计的核心思想。类比游戏引擎：

```
大脑（逻辑层）        修改游戏实体的属性值
  ↓
小脑（物理层 = 运动 + 用户交互） 检测属性变化，同步到物理 IO
  ↓
脑干（硬件层）        实际写入 PLC
```

游戏里你改角色的 `position` 属性，引擎自动把新位置呈现到屏幕上。同理，Haku_EET 里大脑不会直接写 IO 地址——它改 `Car` 的属性，小脑自动把变化同步到物理世界。

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
   │  │ 赋予外设逻辑      │  │ 全局状态+需求        │   │
   │  │ 管理 cron 闹钟   │  │ → 谁去哪             │   │
   │  │ 修改 Car 属性    │  │ 谁变更目的地         │   │
   │  └──────────────────┘  └──────────────────────┘   │
   │  ┌──────────────────┐                             │
   │  │ REPL 控制台       │                             │
   │  │ 文本命令 → 需求   │                             │
   │  └──────────────────┘                             │
   └──────────────────┬───────────────────────────────┘
                      │  修改 Car 属性 / ActionQueue
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
   │  WS bitmap 解析 → 信号名 / 写合并 tick → HTTP     │
   └──────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────┐
   │   cron（外置耳机）                                  │
   │   不属于任何层，只听事件广播                         │
   │   不"数秒"，而是定闹钟                              │
   │   EventRule：'reschedule' 推迟 / 'cancel' 自毁    │
   └──────────────────────────────────────────────────┘
```

### 1.3 大脑（决策层）

大脑内部按职能划分模块，所有模块以事件驱动协作，只操作 `Car` 实体属性，不知道 IO 地址。

**用户交互模块**——给外设赋予逻辑语义。不管是物理按钮、REPL 命令、还是未来的某个外接模块——它把"原始信号"翻译成"电梯需求事件"，修改 `Car` 属性，管理 cron 闹钟。

当前对应代码中的角色：
- `app.py` 里的 IO 事件处理（`_on_cabin_button`、`_on_hall_call_event`、`_on_io_event`）
- `console.py`——REPL 命令（`/car 1 call 5`）转成需求
- `passenger.py`（可选模块）——未来乘客行为抽象

用户交互模块不做调度决策。它只说"有人按了 5 楼"——不说"让 1 号梯去 5 楼"。

**算法层（调度员）**——在知道全局电梯在干什么的情况下，对用户交互模块广播的需求事件做响应调度。

```
输入: 全局电梯状态 + 全局需求
  - 电梯状态：在几楼 / 在两层之间 / 运行中 / 方向
  - 需求：用户交互模块广播的内召/外召/变更目的地事件
输出: 谁去哪 / 谁变更目的地
```

当前实现 `SimpleInternalCall` 还是简化版本（`decide(self, car, pending_calls)`），只看到一部车——这是已知未对齐项。

### 1.4 小脑（物理层：运动 + 用户交互）

**小脑=物理层**。它的核心作用是**抽象电梯各种物理运行参数**（电机、接触器、刹车、反冲、平层、门控、灯光/按钮等），让"`/car 1 call 5`"这种高层命令成为可能——一句话就把电梯送到 5 楼，所有物理实现细节都被隐藏。

下层用户（包括大脑）不需要知道任何电机控制、刹车档位、反冲修正、站点吸附的物理实现细节。

**executor FSM**——维持运动学逻辑。从 ActionQueue 取 Action，展开成具体的 IO 操作序列（拉接触器、继电器、平层检测、站点吸附反冲），维护 `Car.position`、`Car.direction`、`Car.door_state` 等属性。

当前对应：`executor.py`、`controllers.py`

**UI 模块**——负责 `Car.ui` 属性 ↔ 物理 IO 的双向同步。大脑只管改属性，UI 模块负责同步到 IO。

当前对应：`ui.py`、`display.py`

小脑状态：**基本 stable**。"在 REPL 里面已经可以指哪停哪了"—— `/car 1 call 5` 这种高层命令就能驱动所有物理细节。

> **设计哲学参考**：完整的小脑及整体设计哲学清单见 [SPEC.md §13 代码嵌入的设计哲学](./SPEC.md#13-代码嵌入的设计哲学)。

### 1.5 脑干（IO 抽象层）

把物理 IO 抽象为电梯控制点位。往下对接 S7 PLC（通过 IO2HTTP 网关），往给小脑提供信号名级别的 IO 读写。

当前对应：`io_client.py`、`io_mapper.py`、`virtual_plc.py`

**tick 写合并**：脑干的另一关键设计——`io.set(addr, val)` 不会立即 HTTP POST，而是把变化记到内部 buffer，等下一个 tick 周期（默认 20ms = 50Hz）一到，**把一段时间内的所有变化合并成一次 HTTP POST**。

设计动机：
- 一次电机启动需要 4-5 个连续继电器 set。如果每个 set 都立即 HTTP 请求，IO2HTTP 在几毫秒内被连续打 4-5 次往返——非常浪费
- tick flush 把所有变化打包成一帧 PLC 写入，保证 S7 read-modify-write 的原子性，避免中间状态被 PLC 读到

可通过 `config.yaml > io2http.tick_interval_ms` 调整频率。完整设计见 [SPEC.md §4.2](./SPEC.md#42-tick-写合并--避免小包冲击-io2http)。

### 1.6 cron——外置耳机

cron 是一个纯粹的事件驱动闹钟系统。它不属于任何一层，而是作为一个独立组件**挂在事件广播总线上**：

- **监听**：接收 IO 事件（信号名 + car_id + 电平），通过 `EventRule` 判断 reschedule 或 cancel
- **广播**：闹钟到点时广播"叮"事件，由大脑用户交互模块自行决定怎么处理

**关门场景实例**：

```
大脑用户交互模块收到"开门完成"事件 → 调度关门闹钟（10s）
  ├─ 有人按开门按钮 → cancel 闹钟（别关了，人还要进）
  ├─ 按钮松开 → 调度新的关门闹钟（重新数 10s）
  ├─ 光幕一直被挡 → cron reschedule（无限延）
  └─ 闹钟安静走到响 → 大脑用户交互模块监听到
      → 翻译成"需求：关门" → 广播给算法层
      → 算法层调度 Action(CLOSE_DOOR)
      → 小脑 executor 展开关门
```

cron 是"聋子闹钟"——它不判断该不该关，只管理什么时候响。

---

## 2. 设计哲学的 8 条不变量

写代码和写文档时，以下 8 条是**红线**，任何时候不允许违反：

### 不变量 1：三层分离

大脑（决策层）/ 小脑（物理层：运动+用户交互）/ 脑干（IO 抽象层）严格分离。每一层用极致的抽象与解耦联系在一起。不允许跳层。

**反例**：算法层 import io_mapper、小脑直接写 DB 地址、大脑直接拉接触器——都不允许。

### 不变量 2：电梯 = 游戏实体

`Car` 是游戏实体，有属性有数值有状态。绝不包含 IO 地址。

**反例**：往 Car 里加 `io_address`、`plc_tag` 字段。

### 不变量 3：游戏属性驱动

大脑修改 `Car` 属性，小脑自动同步到物理世界。大脑不碰 IO，小脑不碰调度。

### 不变量 4：算法层是调度员

算法层输入全局状态 + 全局需求，输出"谁去哪 / 谁变更目的地"。不做运动学决策，不做 IO 读写。

### 不变量 5：事件驱动

整个系统没有 `asyncio.sleep(N)` 做"等 N 秒后干什么"（那是 cron 的职责），没有 `while True: check(); sleep(0.05)`。

唯一两类"等待"：
1. `wait_for(Event, timeout=...)` —— cron 用，等 deadline 或被事件唤醒
2. `asyncio.Future` —— 站点吸附用，等平层信号偏离后恢复

### 不变量 6：cron 是外置耳机

cron 不属于任何层。它是一个独立的闹钟组件，挂在事件广播总线上。

EventRule 两种模式：
- **reschedule**：事件触发时把闹钟推到 `now + delay`
- **cancel**：事件触发时直接销毁闹钟

### 不变量 7：不外设逻辑不污染调度

用户交互模块只负责"赋予外设逻辑 + 翻译成需求事件"，不做调度决策。
算法层只做调度，不碰外设语义。

### 不变量 8：点位表 = IO 真相来源

`tools/gen_io_config.py` 解析 `点位表.md` 生成 `config/io_config.yaml`。

---

## 3. 模块导航（代码 <-> 架构映射）

### 3.1 按架构层映射

| 架构层 | 代码模块 | 职责 |
|--------|----------|------|
| 大脑 | `core/app.py` | 装配 + 用户交互事件处理 + cron 协作 |
| 大脑 | `core/console.py` | REPL 文本命令 → 需求 |
| 大脑 | `core/passenger.py` | 未来：乘客行为抽象（可选模块） |
| 大脑 | `core/algorithm.py` | 调度员：全局状态+需求 → Action |
| 大脑 | `core/actions.py` | Action 枚举 + ActionQueue（层间语言） |
| 大脑 | `core/player.py` | Car 实体定义（属性/数值/状态） |
| 小脑 | `core/executor.py` | 运动 FSM：取 Action → 展开 IO → 等传感器 |
| 小脑 | `core/controllers.py` | 电机控制、门控制封装 |
| 小脑 | `core/ui.py` | 灯/按钮/显示（用户交互）|
| 小脑 | `core/display.py` | 7 段数码管编码显示 |
| 脑干 | `core/io_client.py` | WS bitmap、HTTP 写合并 tick flush |
| 脑干 | `core/io_mapper.py` | DB <-> I 映射、信号名查表 |
| 脑干 | `core/virtual_plc.py` | 模拟 PLC |
| 外置 | `core/cron.py` | 事件驱动闹钟（不属于任何层） |
| 工具 | `tools/gen_io_config.py` | 点位表 → io_config.yaml |

### 3.2 .qwen/skills 导航（反向索引）

| auto-skill 文件名 | 对应模块 | 说明 |
|-------------------|----------|------|
| auto-skill-elevator-control-arch / -architecture | `core/` 整体 | 电梯控制架构总览 |
| auto-skill-passenger-manager-brain | `core/passenger.py` | 乘客管理——大脑的可选模块 |
| auto-skill-cron-system | `core/cron.py` | 外置耳机——事件驱动闹钟 |
| auto-skill-per-car-io-write | `core/app.py` | 每部车独立的 IO 写通道 |
| auto-skill-change-destination | `core/algorithm.py` | 算法层的"变更目的地"能力 |
| auto-skill-audit-teardown-new-state | `core/app.py` (reset) | 重置/拆卸逻辑 |
| auto-skill-refactor-flag-suppression | `core/player.py` FaultFlags | 故障信号集合 |
| auto-skill-refactor-safety-invariant | `core/executor.py` | 限位 / 急停安全不变量 |

---

## 4. Action 契约表

Action 是大脑（算法层/调度员）的输出语言，也是大脑与小脑之间的通信协议。

### 4.1 Action 一览

来源：`core/actions.py:15-27`

| ActionKind | 发出方 | 参数 | 完成判据 | 小脑展开要点 |
|------------|--------|------|----------|--------------|
| `INITIALIZE` | 大脑算法层（仅当 state==UNKNOWN）| 无 | 触限位反向后平层计数完成，state=READY | 全速跑方向 → 触 1 限位 → 反向逐层平层计数 → 停目标 |
| `MOVE_UP` / `MOVE_DOWN` | 大脑算法层 | 看 car.target_floor | 平层匹配目标楼，direction=IDLE | 启电机方向+速度 → 过平层 → 减速刹车 → 反冲 |
| `OPEN_DOOR` | 大脑用户交互模块翻译的需求 | 无 | door_open_done 信号到位 | 拉开门继电器 → 等开门到位 → cron 兜底 8s |
| `CLOSE_DOOR` | 大脑用户交互模块（cron 闹钟响后）| 无 | door_close_done 信号到位 | 拉关门继电器 → 等关门到位 → cron 兜底 8s |
| `SET_DISPLAY` | 大脑 | floor 或 glyph | IO 写入完成 | 查 floor_display → 写 segment_* 位 |
| `EMERGENCY_STOP` | 小脑（限位触发时自广播）| 无 | 全部 output 归零 | 停电机 + 全刹 + 清所有输出 |
| `RESET_FAULT` | 大脑（/clear 等命令）| 无 | FaultFlags 全清 | 清故障指示 + 恢复 ready |
| `NOOP` | 任意 | 无 | 立即 done | 占位 / 心跳 |

### 4.2 关键语义约束

- **MOVE_UP / MOVE_DOWN 不碰门**：算法层只负责把车开到目标楼，开关门是另一个独立子系统。
- **OPEN_DOOR / CLOSE_DOOR 不碰电机**：门控和运动在 executor 内部互斥（运动中拒绝开门）。
- **INITIALIZE 期间锁所有**：初始化过程中不响应其他 Action。
- **EMERGENCY_STOP 最高优先级**：任何层广播急停事件，小脑收到后立即全输出归零。

---

## 5. 端到端调用链（事件驱动视角）

以"内召 5 楼"为例，展示事件如何在各层间流动：

```
用户按下轿内 5 楼按钮（物理 PLC）
  ↓
脑干（IO 抽象层）：
  IOClient 收 WS bitmap → IOEvent(1, 'cabin_button_5', True)
  → 广播（事件总线）
  ↓
小脑 UI 模块 → 大脑用户交互模块：
  收到 IO 事件 → 翻译成"1 号梯的需求：去 5 楼"
  → pending_calls[1].append(5)
  → 广播需求事件
  ↓
大脑算法层（调度员）：
  _tick() 被事件唤醒
  → algorithm.decide(car, pending_calls)
  → return [Action(MOVE_UP)]  输出：谁去哪
  → action_queues[1].put(MOVE_UP)
  ↓
小脑 executor：
  从 ActionQueue 取到 MOVE_UP
  → 拉上行接触器 + 高速接触器 + 电机启动
  → 监听平层信号
  → 到达 5 楼 → 减速刹车 + 站点吸附（反冲）
  → 更新 Car.position = 5, Car.direction = IDLE
  → 广播"动作完成"事件
  ↓
大脑用户交互模块监听到：
  → 清 pending_calls[1] 中的 5
  → 翻译"到站"需求 → 决定是否需要 OPEN_DOOR
  ↓
（后续由 passenger_flow / cron 继续处理关门等）
```

全程没有 `sleep()`、没有 `while: poll`、没有跨层硬调用——所有联系都通过事件广播。

---

## 6. 已知雷区

### 6.1 station_hold 不稳定

来源：`.qwen/memory/project/station-hold-unstable.md`，提交 `5fbb9fa`

- **confirmed fixed**：手动模式 `[DEBUG] 拉扯` 不再刷屏（paused check）
- **confirmed fixed**：auto_seek 跳过手动车（UNKNOWN + not paused）
- **unconfirmed**：纯事件驱动站点吸附在实际 VPLC/PLC 环境中是否正确
- **unconfirmed**：`_level_hold_settling` 稳定窗口机制是否可靠
- **下次开始点**：`/car all init down 1` + `/debug show station_hold`

### 6.2 2026-07-08 Code Review — Verified Critical（6 条）

来源：`.qwen/reviews/2026-07-08-153000-full-project.md`

| # | 问题 | 文件位置 | 现象 | 修复方向 |
|---|------|----------|------|----------|
| 1 | `ActionQueue.get_nowait()` 缺失 | `actions.py:60-76` + `app.py:661` | `/reset` 时 AttributeError 崩溃 | 加 get_nowait 方法 |
| 2 | `test_executor.py` 18 个同名重复定义 | `tests/test_executor.py` | 32 def 仅收集 14 个 | 删重复定义 |
| 3 | `floor_display` 配置是死代码 | `display.py` + `executor.py` | 配置改了 LED 不变 | show_number 查 floor_display |
| 4 | 全局信号误路由到当前轿厢 | `app.py:246-251` | 0 为 falsy 取错 cid | 改判断逻辑 |
| 5 | executor 热路径冗余 mapper 查询 | `executor.py:155-220` | 每次 IO 事件多次查表 | 预解析 level 信号 |
| 6 | Bitmap 全 800 位扫描 + idle 唤醒 | `io_client.py:159-167,262-307` | 无变化空转 | changed 短路 + 降频 |

### 6.3 未单独验证的 Critical

| # | 问题 | 位置 |
|---|------|------|
| 7 | CLOSE_DOOR 完成后 MOVE 被双重 dispatch | `app.py:380-403` + `passenger.py:269-291` + `executor.py:683-702` |
| 8 | asyncio.create_task 吞异常（9+ 处） | `app.py:731/746/1114`, `passenger.py:444`, `virtual_plc.py:246/318`, `io_client.py:129`, `cron.py:140` |
| 9 | WS reconnect 静默吞连接错误 | `io_client.py:265-275` |
| 10 | cron stop 后 listener 泄漏 | `cron.py:127-141, 219-227` |

### 6.4 赛前硬件验证 checklist

来源：[SPEC.md §3.4](./SPEC.md#34-硬件契约赛前必验)。

比赛现场需要逐条验证以下硬件假设：

| # | 验证项 | 预期行为 | 若不符合 |
|---|--------|----------|--------|
| 1 | 电磁刹车极性 | `brake_X = 0` 释放，`brake_X = 1` 刹死 | 反转 `set_brakes()` 内部 0/1 映射 |
| 2 | 限位开关常闭/常开 | 2 限位触发 → EMERGENCY_STOP | 反转限位触发逻辑 |
| 3 | 电机接触器极性 | 正逻辑（1=吸合） | 改 MotorController 内部映射 |
| 4 | 门到位信号极性 | door_open/close_done = 1 到位 | 反转到位判断逻辑 |
| 5 | 平层信号电平 | level_up & level_down 到位 = 1 | 反转平层到达判断 |
| 6 | 光幕信号电平 | light_curtain = 1 遮挡中 | 反转遮挡判断 |

> 验证方法：在 REPL 中执行 `/car N debug on` 观察 IO 信号值，对照 [IO_UI.md](./IO_UI.md) 确认。每条假设确认后记录到比赛日志。

---

## 7. 业务约束

- **楼层**：正常使用 1-10 层，上基站 11 层，下基站 0 层
- **轿厢**：最多 6 部，car_ids 启动时确定，/reload 不支持增删
- **算法**：首版 SimpleInternalCall 只响应内召
- **初始化**：IO 缓存为空时禁止 INITIALIZE，需先操作一个按钮触发 bitmap 推送
- **IO2HTTP 必须先于 Haku_EET 启动**

---

## 8. 进一步阅读

| 文档 | 内容 | 谁该读 |
|------|------|--------|
| `SPEC.md` | 设计规格真相来源（架构 + 契约 + 路线图）| **所有决策以此为据** |
| `README.md` | 目录结构、快速开始、安装、测试 | 所有人都该读 |
| `COMMAND_MANUAL.md` | REPL 命令手册 + 架构奇技淫巧深读 | 日常开发常备 |
| `IO_UI.md` | IO 输出信号 DB 地址 ↔ UI 模块映射 | 改点位/UI 时必读 |
| `点位表.md` | IO 信号原始表 | 改硬件接线时必读 |
| `.qwen/MEMORY.md` | 不稳定标记快速索引 | 接班 AI 每次启动读 |
| `.qwen/skills/` | 按 3.2 节导航对照 | 深入特定模块时查阅 |
| `.qwen/reviews/*.md` | 代码审查完整报告 | 改到相关区域时查阅 |
