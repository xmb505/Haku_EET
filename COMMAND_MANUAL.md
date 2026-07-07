# Haku_EET REPL 命令手册

所有命令以 `/` 开头（MC 风格）。输入 `/help` 随时查看。

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

# 架构奇技淫巧

## Cron —— 事件驱动延时定时器

`core/cron.py` 是一个纯事件驱动的内部定时器，不是普通的 `asyncio.sleep`。

### 核心概念

```
CronJob（任务）
  ├─ trigger_time    触发绝对时间
  ├─ action          触发时执行的回调
  ├─ auto_remove     自毁：触发后自动清除
  └─ event_rules     哪些 IO 事件影响它

EventRule（事件规则）
  ├─ signal_name     IO 信号名（'light_curtain' / 'cabin_button_3'）
  ├─ car_id          哪部车
  ├─ action          'reschedule' | 'cancel'
  └─ delay           仅 reschedule：新延时
```

### 数据结构

```
_jobs:          name → CronJob           （所有活跃任务）
_heap:          [(trigger_time, name)]   （小顶堆，最近要触发的排最前）
_event_idx:     (car_id, sig) → [(job_name, action, delay)]
                                         （IO 事件直达任务，O(1) 查询）
_wakeup_event:  asyncio.Event            （零轮询，只有事件才唤醒）
```

### run 循环（纯事件驱动，零轮询）

```
while running:
  fire 所有已过期的 job → 自毁 auto_remove
  wait_for(wakeup_event, timeout=next_deadline)
```

只在这三种情况下唤醒：
1. **定时到期**（`wait_for` timeout）→ fire job
2. **新任务 schedule**（`wakeup_event.set()`）→ 可能比当前等待更早
3. **IO 事件触发规则**（`_on_io_event` → `wakeup_event.set()`）→ 重调度/取消

### 两个魔法规则

**reschedule（延时事件）：** IO 信号触发时，把任务推到 `now + delay`。

> 例子：关门定时 10s。光幕被触发 → cron 把关门推迟到「光幕触发那一刻 + 10s」。
> 反复触发光幕 = 反复推迟关门，永远不会在有人穿过时夹到。

**cancel（自毁事件）：** IO 信号触发时，直接销毁任务。

> 例子：熄灯定时 10min。内部按钮按下 → cron 自毁熄灯任务。
> 有人在轿厢里，灯不该灭。10min 计时归零，等人出去关门后重新计时。

### 生命周期

```
schedule(job) → 入堆 + 建 event_idx → wakeup
cancel(name)  → 删 _jobs + 删 event_idx + 删 _latest_trigger
                （heap 里 old entry 懒删除——fire 时看到 name 不在 _jobs 里就跳过）
stop()        → 清所有数据
```

---

## human_presence —— 人类存在推测状态机

每部电梯的 `Car.human_presence` 推测轿厢内是否有人。不需要摄像头、红外传感器，**纯逻辑推断**。

### 三态定义

| 值 | 含义 | 何时进入 |
|----|------|---------|
| `-1` | 确定无人 | INIT 完成 |
| `0` | 不确定 | 由 passenger_flow 模块推算 |
| `1` | 确定有人 | 内部按钮被按下 |

### 控制层（app.py）只负责两处更新

```
INITIALIZE 完成                              → -1（车厢空的）

cabin_button_X 上升沿（IO listener）         → +1  = 1
```

其余过渡（关门后 → 0、熄灯 → -1 等）由上层 **passenger_flow 模块** 通过事件监听处理。当前 passenger_flow 模块尚未实现，所以：
- INIT 后轿厢自动视为无人
- 按过内部按钮之后轿厢视为有人，直到下一次 INIT

**实现 passenger_flow 时**：观察 `_on_action_done`（动作完成事件）和 IO 事件，注册 cron job 即可。可以参考此前的 `_schedule_close_door` / `_schedule_lights_off` 设计（已从 app.py 移除）。

---

## 分层承诺 —— 不允许跳层

每一层都是单向、不可跳过的：

```
IOEvent（电平变化）
  → Listener（事件监听器，不摸 IO）
    → App API（高层函数，如 call_internal）
      → ActionQueue（异步队列）
        → Executor（硬件 FSM，展开动作为 IO 序列）
          → Controller（MotorController / DoorController）
            → IOClient（写合并缓冲区，tick flush）
              → HTTP POST（物理 IO）
```

- **Listener 是纯事件驱动** —— 不主动 poll、不直接读写 IO
- **App API 是电梯函数** —— 只传「去哪里、干什么」，不管 IO 地址
- **Executor 是 FSM** —— 把「开门」展开为「设继电器 → 等传感器到位」
- **Controller 是硬件封装** —— 不单独写接触器位，调 `motor.start(direction, speed)`
- **IOClient 是物理层** —— tick 合并写、WS bitmap 解析，上层不碰

> 例：按下 1 楼外召 → `_on_hall_call_event`（不摸 IO）
> → `_dispatch_hall_call`（纯函数）
> → `call_internal(1, origin='hall')`（加 pending）
> → `_tick` → `algorithm.decide` → `ActionQueue`
> → executor MOVE → 到站 → push OPEN_DOOR → executor open → `_on_action_done`
> → `_handle_algorithm_state_change`（清 pending，不做 passenger flow）
>
> 全程没有任何地方直接写 IO 地址、没有任何 `time.sleep`、没有任何轮询 while 循环。
> 乘客流程（自动关门、自动熄灯）由 passenger_flow 模块通过事件监听自行注入——它有权看到一切 IO 事件和动作事件，但它不属于控制层。

---

## 为什么不用 time.sleep

整个系统只有两类"等待"：

1. **`asyncio.wait_for(Event, timeout=...)`** —— 给 cron 用。等下一个 deadline 或被事件唤醒。
2. **`asyncio.Future`** —— 给站点吸附用。等平层信号偏离后恢复。

没有 `asyncio.sleep(X)` 做"等 N 秒后干什么"——那是 cron 的职责。
没有 `while True: check_something(); sleep(0.05)` ——那是事件驱动的反面。

**"事件驱动"不是技术选择，是架构承诺。**
