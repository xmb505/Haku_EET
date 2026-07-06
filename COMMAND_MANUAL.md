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
| `-1` | 确定无人 | INIT 完成、熄灯后无操作 |
| `0` | 不确定 | 有过外召活动但无内召确认 |
| `1` | 确定有人 | 内部按钮被按下 |

### 状态机

```
INITIALIZE done                             → -1（刚初始化，车厢空的）

外召 → 开门 → 关门                         →  0（有人按了外召，但不确定进没进）

内部按钮按下                                →  1（人在里面，不用怀疑）

关门后结算：
  state == 1  →  0    （人可能出去了）
  state == 0  →  0    （不确定，开始节能倒计时）
  state == -1 → -1    （没人来过）

熄灯 cron 到期（10min 无活动）              → -1（节电成功）

熄灯后内部按钮按下                          → +2 = 1（-1 → 1，有人）
```

### 熄灯节能 = human_presence + cron 配合

```
CLOSE_DOOR done
  ├─ human_presence == 0 → schedule(lights_off, 10min)
  └─ human_presence 其他 → 不注册（有人/无人不需要）

lights_off cron fires → push LIGHT_OFF + human_presence = -1

内部按钮按下 → cancel(lights_off) + human_presence = 1
开门         → cancel(lights_off)（人要进来了）
```

**极致之处：** 如果有人在里面躲了 10min 不动，灯灭了。
他伸手按按钮 → cron 自毁 → `-1 + 2 = 1`（确定有人）→ 灯亮了 → 电梯正常运行。
全程无传感器、无轮询、无浪费。

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
> → `cron.schedule(close_door, 10s, reschedule=[light_curtain])`
> → （光幕触发 → cron 重调度）|（到期 → push CLOSE_DOOR）
> → executor close → `_on_action_done` → `_schedule_lights_off` → ...
>
> 全程没有任何地方直接写 IO 地址、没有任何 `time.sleep`、没有任何轮询 while 循环。

---

## 为什么不用 time.sleep

整个系统只有两类"等待"：

1. **`asyncio.wait_for(Event, timeout=...)`** —— 给 cron 用。等下一个 deadline 或被事件唤醒。
2. **`asyncio.Future`** —— 给站点吸附用。等平层信号偏离后恢复。

没有 `asyncio.sleep(X)` 做"等 N 秒后干什么"——那是 cron 的职责。
没有 `while True: check_something(); sleep(0.05)` ——那是事件驱动的反面。

**"事件驱动"不是技术选择，是架构承诺。**
