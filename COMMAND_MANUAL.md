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
