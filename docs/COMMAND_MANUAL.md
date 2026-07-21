# Haku_EET REPL 命令手册

所有命令以 `/` 开头（MC 风格）。输入 `/help` 随时查看。

> 设计规格真相来源：[SPEC.md](./SPEC.md) | 架构总纲：[HANDOVER.md](./HANDOVER.md)

## 目录

**命令速查（上半篇）**
- [轿厢控制 `/car`](#轿厢控制-car)
- [调试控制](#调试控制)
- [UI 模块 `/ui` `/buttonui`](#ui-模块-ui-buttonui)
- [手动门 `/door`](#手动门-door)
- [模块开关 `/module`](#模块开关-module)
- [退出](#退出)
- [`/help`](#help)
- [典型调试会话](#典型调试会话)

**架构奇技淫巧（下半篇）**
- [大脑/小脑/脑干 —— 分层承诺](#大脑小脑脑干--分层承诺)
- [Action 契约表](#action-契约表)
- [Cron —— 事件驱动闹钟（外置耳机）](#cron--事件驱动闹钟外置耳机)
- [human_presence —— 人类存在推测（Car 属性）](#human_presence--人类存在推测car-属性)
- [事件广播总线](#事件广播总线)
- [为什么不用 time.sleep](#为什么不用-timesleep)

---

## 轿厢控制 `/car`

所有操作通过 `/car` 统一入口，支持 `Tab` 补全。

### `/car <id> init [<up|down> [<floor>]]`

手动触发初始化。参数可选，缺省使用 config 默认值。

```
haku> /car 1 init
1 号梯初始化的完整流程：
  全速朝方向跑 → 触 1 限位 → 反向 → 逐层完美平层计数 → 停在目标楼层

haku> /car 1 init up 3
上行触顶后反向计数到 3 楼

haku> /car 1 init down 5
下行触底后反向计数到 5 楼
```

程序启动时不会自动初始化——IO 缓存为空时禁止 INITIALIZE，防止误判限位信号撞 2 限位。
必须先操作一个按钮（如轿内选层按钮），触发 IO2HTTP 推送完整 I 区 bitmap 后再执行 init。

### `/car <id> call <floor>`

内召：到目标楼层（1-10）。

```
haku> /car 1 call 5
```

算法只发 MOVE_UP/MOVE_DOWN，不碰门开关——call 是调试运动用的，门控是另一个子系统。

### `/car <id> status`

查看轿厢状态：

```
haku> /car 1 status
算法:        simple_internal_call
模拟模式:    True
初始化方向:  up
轿厢 ID:     1
状态:        ready
当前位置:    L5
方向:        idle
门状态:      open
目标楼层:    -
显示:        5
动作队列:    0
待处理召唤:  []
故障:        无
```

### `/car <id> manual`

进入手动控制模式（WASD 风格），executor 暂停，可以撞限位看 PLC 反应。

```
haku> /car 1 manual

==================================================
  car 1 手动控制模式（executor 暂停，可撞限位）
  ↑ ↓ / ← →   = 上下行（低速）
  Shift+↑↓    = 上下行（高速）
  空格         = 立即停 + 刹车
  数字键 1-7   = 设置刹车档位（0=释放, 7=全刹）
  ESC / q      = 退出手动控制
  退出会恢复 executor 2 限位保护
==================================================
[car 1] L=1 LOW  方向=· 门=关 刹车=0 正常
```

松开方向键 ≈ 立即停电机（100ms 内），靠"上次按键后无新输入 = 松开"近似模拟。

### `/car <id> auto`

切回自动控制：释放刹车、停电机、算法接管。

---

## 调试控制

### `/debug on` / `/debug off`
开启/关闭调试日志（tick 输出 + executor 状态变化）。

### `/clear`
将所有输出位置零（清 DB11 所有信号，不含 ready 信号）。

### `/reload`
重新读 `config/config.yaml`、`config/io_config.yaml`、`config/display_config.yaml`。无需重启程序。

---

## UI 模块 `/ui` `/buttonui`

UI 模块（`core/ui.py`）管理轿厢可视指示灯 + 内召按钮灯 + 外召按钮灯。它是逻辑层——`Car.ui` 维护状态，`UiController` 负责把状态同步到 IO。

注意：**UI 模块不负责开关门指示灯**——PLC 上没有独立的 `door_open_indicator` / `door_close_indicator` 输出点，开关门视觉显示由电机继电器本身（硬件物理效果）承担。

### `/ui <car> <field> <on|off>`

设置轿厢单个 UI 字段（`Car.ui`）。

```
haku> /ui 1 full_load true       # 满载指示灯亮
haku> /ui all warn true         # 所有轿厢故障指示灯亮
haku> /ui 1 fan true            # 打开风扇
haku> /ui 1 light false         # 关闭轿厢灯
haku> /ui 1 light               # 省略 true/false → toggle 自身当前状态
```

支持的 `<field>`：`full_load` / `fault` (= warn) / `light` / `fan`

支持的车号：`<id>` 或 `all`

**省略 `true/false` 时**取反当前状态(toggle)。

### `/buttonui in <car> <floor> <on|off>`

点亮轿内某层按钮灯。`<car>` 和 `<floor>` 都支持批量（`,` 列表或 `1-10` 范围或 `all`）。

```
haku> /buttonui in 1 5 true                  # 1 号梯 5 楼内召灯亮
haku> /buttonui in 1 1,2,3,4,5,6,7,8,9,10 true   # 1 号梯 1-10 楼灯全亮
haku> /buttonui in 1,2,3 1-10 true            # 1-3 号梯所有内召灯亮（笛卡尔积）
haku> /buttonui in all all false              # 所有轿厢所有内召灯灭
haku> /buttonui in 1 1                        # 省略 true/false → toggle 1 号梯 1 楼
```

### `/buttonui out <floor> <direction> <on|off>`

点亮外召按钮灯。`<floor>` 支持批量。`<direction>` 是 `up` / `down`（分别对应上行/下行指示灯）。

```
haku> /buttonui out 5 up true                # 5 楼下行上灯亮
haku> /buttonui out 1-10 down false          # 清空所有下行外召灯
haku> /buttonui out 1,3,5 up                 # 省略 true/false → 逐个 toggle
```

注意边界：1 层没有 `down_1`、10 层没有 `up_10`（PLC 点位表里就不存在）——批量命令会自动跳过非法方向。

**所有 `/ui` `/buttonui` 命令省略 `true/false` 时,逐个 toggle 自身当前状态**。

---

## 手动门 `/door`

```
haku> /door 1 open                # 1 号梯开门（非阻塞）
haku> /door 1 close               # 1 号梯关门
haku> /door 1 open force          # 强制开门（即使电梯在运动中）
```

**事件驱动、非阻塞**：`/door` 立即返回 `dispatched` / `force_done` / `rejected` / `busy`，后台 task 跟踪门动作完成。

监视门完成状态：

```
haku> /debug show door_status     # 开 → 监听 door_open_done / door_close_done 事件
                                 # 关 → 停止监听
                                 # 正常完成不打印，出错（开错层）才打印 ⚠️
```

边界：
- 拒运行中（即使 force 也拒绝——用户在动就让他动完）
- 拒已初始化失败（FAULIT 锁状态）
- 强制互斥（同时两次 `/door` 同轿厢：第二次返回 `busy`）
- 错层检测：开门时若锁信号指示不在预期楼层，打印警告

---

## 模块开关 `/module`

```
haku> /module                      # 列出所有可选模块及开关状态
haku> /module station_seek true    # 启用站点吸附
haku> /module station_seek false   # 关闭站点吸附
```

模块独立运作，关闭时占用资源最小、影响为零。默认全部关闭。

---

## 退出

### `/quit`
退出 REPL，程序优雅关闭（停止 executor 后台任务、关闭 IOClient）。也可按 `Ctrl-C` 或 `Ctrl-D`。

---

## /help

显示命令列表。

---

## 典型调试会话

### 模拟模式无硬件

```bash
$ python3 -m core --simulate
```

进入 REPL 后：

```
haku> /car 1 init down

haku> /car 1 status
轿厢 ID:     1
状态:        ready
当前位置:    L1

haku> /car 1 call 5

haku> /car 1 status
方向:        up
目标楼层:    L5

haku> /car 1 status
当前位置:    L5
方向:        idle

haku> /quit
再见
```

### 实机模式

```bash
$ python3 -m core    # 不带 --simulate，连 IO2HTTP 192.168.1.201

haku> /car 1 status      # 看到的是真实 IO 状态
haku> /car 1 call 5      # 内召 5 楼
haku> /debug on           # 观察每个 tick 输出
```

---

 架构奇技淫巧（基于大脑/小脑/脑干三层）

完整设计规格见 [SPEC.md](./SPEC.md)。

## 大脑/小脑/脑干 —— 分层承诺

每一层都是单向、不可跳过的：

```
IO电平变化
  → 脑干处理（信号名解析）→ 广播事件
    → 小脑 UI 模块更新 Car 属性 → 广播属性变化
      → 大脑用户交互模块翻译为需求事件 → 广播
        → 大脑算法层调度 → Action → 入 ActionQueue
          → 小脑 executor 展开 FSM → IO 操作
            → 脑干写入（HTTP POST）
```

各层承诺：
- **大脑只改 `Car` 属性** —— 不碰 IO 地址，不直接调小脑
- **小脑负责双向同步** —— IO 电平变化 → `Car` 属性更新 / `Car` 属性变化 → IO 写入
- **脑干只做物理传输** —— WS bitmap 收 / HTTP 发，不触碰任何电梯语义

> 例：按下 5 楼内召
> → 脑干收 WS → 广播 IO 事件
> → 小脑更新 Car 属性 → 广播属性变化
> → 大脑用户交互模块翻译为需求 → 广播
> → 大脑算法层 decide() → Action(MOVE_UP) → ActionQueue
> → 小脑 executor 展开 → 拉接触器 → 启电机 → 追平层
>
> 全程没有跨层硬调用，没有 time.sleep，没有轮询。

---

## Action 契约表

Action 是大脑（算法层/调度员）的输出语言，也是大脑与小脑之间的通信协议。

来源：`core/actions.py:15-27`

| ActionKind | 发出方 | 完成判据 | timeout |
|------------|--------|----------|---------|
| `INITIALIZE` | 大脑算法层（仅当 state==UNKNOWN）| 触限位反向后平层计数完成，state=READY | 隐含 |
| `MOVE_UP` / `MOVE_DOWN` | 大脑算法层 | 平层匹配目标楼，direction=IDLE | 反冲可能额外几秒 |
| `OPEN_DOOR` | 大脑用户交互模块 | door_open_done 信号到位 | cron 兜底 8s |
| `CLOSE_DOOR` | 大脑用户交互模块（cron 闹钟响后）| door_close_done 信号到位 | cron 兜底 8s |
| `SET_DISPLAY` | 大脑 | IO 写入完成 | 无（同步） |
| `EMERGENCY_STOP` | 小脑（限位/急停触发）| 全部 output 归零 | 无（立即） |
| `RESET_FAULT` | 大脑 | FaultFlags 全清 | 无（立即） |
| `NOOP` | 任意 | 立即 done | 无 |

语义约束：
- MOVE 不碰门，OPEN_DOOR/CLOSE_DOOR 不碰电机
- INITIALIZE 期间锁所有其他 Action
- EMERGENCY_STOP 最高优先级

---

## Cron —— 事件驱动闹钟（外置耳机）

`core/cron.py` 是一个独立的事件驱动闹钟系统。它**不属于任何层**，悬挂在事件广播总线上。

核心哲学：**不是数秒，是定闹钟。**

为什么不是 sleep？

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

### 核心概念

```
CronJob（任务）
  ├─ trigger_time    闹钟响的绝对时间
  ├─ action          闹钟响时执行的回调
  ├─ auto_remove     触发后自动清除
  └─ event_rules     哪些事件影响这个闹钟

EventRule（事件规则）
  ├─ signal_name     IO 信号名（'light_curtain' / 'cabin_button_3'）
  ├─ car_id          哪部车
  ├─ action          'reschedule' | 'cancel'
  └─ delay           仅 reschedule：新延时
```

### 数据结构

```
_jobs:          name → CronJob           所有活跃任务
_heap:          [(trigger_time, name)]   小顶堆，最近要触发的排最前
_event_idx:     (car_id, sig) → [(job_name, action, delay)]
                                          IO 事件直达任务，O(1) 查询
_wakeup_event:  asyncio.Event            零轮询，只有事件才唤醒
```

### run 循环

```
while running:
  fire 所有已过期的 job → 自毁 auto_remove
  wait_for(wakeup_event, timeout=next_deadline)
```

只在这三种情况下唤醒：
1. **定时到期**（`wait_for` timeout）→ fire job
2. **新任务 schedule**（`wakeup_event.set()`）→ 可能比当前等待更早
3. **IO 事件触发规则**（`_on_io_event` → `wakeup_event.set()`）→ 重调度/取消

### 两种事件规则

**reschedule（推迟）：** IO 信号触发时，把闹钟推到 `now + delay`。

> 场景：关门定时 10s。光幕被挡 → cron 把关门推迟到「光幕触发那一刻 + 10s」。
> 反复触发光幕 = 反复推迟关门，永远不会在有人穿过时夹到。

**cancel（自毁）：** IO 信号触发时，直接销毁任务。

> 场景：熄灯定时 10min。内部按钮按下 → cron 自毁熄灯任务。
> 有人在轿厢里，灯不该灭。

### 生命周期

```
schedule(job) → 入堆 + 建 event_idx → wakeup
cancel(name)  → 删 _jobs + 删 event_idx
                （heap 里 old entry 懒删除——fire 时看到 name 不在 _jobs 里就跳过）
stop()        → 清所有数据
```

### cron 与各层的关系

cron 不"属于"任何层——它是挂在事件总线上的外置耳机：
- **监听**：所有层的事件广播 → 通过 EventRule 决定是否 reschedule/cancel
- **广播**：闹钟到点 → 广播"叮" → 大脑用户交互模块决定怎么处理

关门场景：
```
大脑用户交互模块收到"开门完成" → 调度关门闹钟（10s）
  ├─ 有人按开门按钮 → cancel 闹钟
  ├─ 按钮松开 → 调度新关门闹钟
  ├─ 光幕被挡 → reschedule（无限延）
  └─ 闹钟走到响 → 大脑监听到 → 翻译为"需求：关门"
```

---

## human_presence —— 人类存在推测（Car 属性）

`Car.human_presence` 是 Car 实体的一个属性，由**大脑用户交互模块**维护。

不需要摄像头、红外传感器，**纯逻辑推断**。

### 三态定义

| 值 | 含义 | 何时进入 |
|----|------|---------|
| `-1` | 确定无人 | INIT 完成 |
| `0` | 不确定 | 由 passenger_flow 模块推算 |
| `1` | 确定有人 | 内部按钮被按下 |

### 当前更新逻辑

```
INITIALIZE 完成                              → -1（车厢空的）
cabin_button_X 上升沿（IO listener）         → +1  = 1
```

其余过渡（关门后 → 0、熄灯 → -1 等）由上层 **passenger_flow 模块** 通过事件监听处理。当前 passenger_flow 模块尚未实现，详见 [SPEC.md §11 路线图](./SPEC.md#11-待办与路线图)。

---

## 事件广播总线

所有层之间通过事件广播通信，不直接调用。事件总线通过 Listener 模式实现。

### 事件类型

| 事件类型 | 发出方 | 消费者 |
|----------|--------|--------|
| IO 电平变化 | 脑干 | 小脑 → 大脑 |
| 需求事件 | 大脑用户交互模块 | 大脑算法层 |
| Action | 大脑算法层 → ActionQueue | 小脑 executor |
| Action 完成 | 小脑 executor | 大脑用户交互模块 |
| 闹钟响 | cron | 大脑用户交互模块 |
| Car 属性变化 | 小脑 | 大脑 |

### Listener 注册

```python
# 小脑注册监听 IO 信号
io.add_listener(signal_name='cabin_button_5', car_id=1, callback=...)

# 大脑注册监听 Action 完成
executor.on_action_done = user_interaction._on_action_done
```

Listener 是纯事件驱动的——不主动 poll、不直接读写 IO。

---

## 为什么不用 time.sleep

整个系统只有两类"等待"：

1. **`asyncio.wait_for(Event, timeout=...)`** —— 给 cron 用。等下一个 deadline 或被事件唤醒。
2. **`asyncio.Future`** —— 给站点吸附用。等平层信号偏离后恢复。

没有 `asyncio.sleep(X)` 做"等 N 秒后干什么"——那是 cron 的职责。
没有 `while True: check_something(); sleep(0.05)` ——那是事件驱动的反面。

**"事件驱动"不是技术选择，是架构承诺。**
