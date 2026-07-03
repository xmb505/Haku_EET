# Haku_EET REPL 命令手册

所有命令以 `/` 开头（MC 风格）。输入 `/help` 随时查看。

---

## 系统查询

### `/help`
显示命令列表。

### `/status`
显示电梯玩家当前状态：
```
算法:        simple_internal_call
模拟模式:    True
初始化方向:  down
轿厢 ID:     1
状态:        ready
当前位置:    L5
方向:        idle
门状态:      open
目标楼层:    L5
显示:        5
动作队列:    0
待处理召唤:  [5]
故障:        无
```

---

## 初始化

### `/init [down|up]`
手动触发初始化。

- 不带参数：使用 `config/config.yaml` 里的 `initialization_direction`
- 带参数 `down`/`up`：临时切换方向（不写入 config），下次 `/reload` 会还原

程序启动时会**自动**发一次 INITIALIZE，按 config 里的方向执行。

---

## 调度任务

### `/call <floor>`
内召：到目标楼层（1-10）。例：
```
haku> /call 5
已内召 L5
```

可连续发多个召唤，算法按 FIFO 处理：
```
haku> /call 3
haku> /call 8
haku> /status   # pending=[3, 8]，先去 3 楼再去 8 楼
```

---

## 算法管理

### `/algo list`
列出可用算法，当前算法带 `← 当前` 标记。

### `/algo show`
显示当前算法名。

### `/algo set <name>`
热切换算法，立即生效：
```
haku> /algo set simple_internal_call
已切换到算法: simple_internal_call
```

加新算法：在 `core/algorithm.py` 加新类 + 加入 `ALGORITHM_REGISTRY`，重启程序即可发现。

---

## 显示控制（调试用）

### `/display <floor|up|dn|fault|A>`
手动设置 7 段数码管显示。

- `/display 5` —— 显示楼层 5
- `/display 10` —— 显示 10 楼（按 `display_config.yaml` 的 `floor_display.10` 映射）
- `/display up` —— 显示上行箭头
- `/display dn` —— 显示下行箭头
- `/display fault` —— 显示故障码
- `/display A` —— 自定义字符（需在 display_config 的 glyphs 里定义）

---

## 模拟输入（仅 `--simulate` 模式）

### `/sim input <signal> <0|1|toggle>`
模拟一个输入信号变化，相当于手工"触发" PLC 推上来的电平。

例：
```
haku> /sim input bottom_limit_1 1     # 模拟下行端站限位
haku> /sim input overload toggle      # 翻转超重信号
```

可用信号名（部分常用）：
- `bottom_limit_1` / `bottom_limit_2` / `top_limit_1` / `top_limit_2` —— 端站限位
- `level_up` / `level_down` —— 上/下平层
- `door_open_done` / `door_close_done` —— 门到位
- `door_open_button` / `door_close_button` —— 内召开门/关门按钮
- `overload` / `service_mode` / `light_curtain` —— 故障标志
- `cabin_button_1` ~ `cabin_button_10` —— 轿内选层按钮

### `/sim position <floor>`
直接修改轿厢位置（跳过物理移动），用于调试算法决策逻辑。

---

## 调试

### `/debug on` / `/debug off`
开启/关闭调试日志（tick 输出 + executor 状态变化）。

### `/actions`
查看动作队列当前长度。

---

## 配置管理

### `/reload`
重新读 `config/config.yaml`、`config/io_config.yaml`、`config/display_config.yaml`：
- 修改初始化方向 → `/reload`
- 修改 10 楼显示规则 → `/reload`
- 修改 IO 地址映射 → 先重跑 `gen_io_config.py` 再 `/reload`

无需重启程序。

---

## 退出

### `/quit`
退出 REPL，程序会优雅关闭（停止 executor 后台任务、关闭 IOClient）。

也可按 `Ctrl-C` 或 `Ctrl-D`。

---

## 典型调试会话

### 模拟模式无硬件

```bash
$ python3 -m core --simulate

============================================================
  Haku_EET  西门子杯电梯控制离散算法  REPL
============================================================
输入 /help 查看命令列表

haku> /status
算法:        simple_internal_call
模拟模式:    True
初始化方向:  down
轿厢 ID:     1
状态:        unknown      ← 还没初始化
当前位置:    ?
方向:        idle
门状态:      closed
目标楼层:    -
显示:        1
动作队列:    0
待处理召唤:  []
故障:        无

haku> /sim input bottom_limit_1 1     ← 模拟触发端站限位（轿厢跑到 1 楼基站）
已模拟 bottom_limit_1 (I8.2) = 1

haku> /status
状态:        ready         ← 已就绪
当前位置:    L1
显示:        1

haku> /call 5
已内召 L5

haku> /status
方向:        up            ← 已经在上行
目标楼层:    L5

haku> /sim input level_up 1           ← 模拟到达 5 楼平层
已模拟 level_up (I7.6) = 1

haku> /status
当前位置:    L5
方向:        idle
门状态:      opening

haku> /sim input door_open_done 1     ← 模拟门开到位
已模拟 door_open_done (I7.4) = 1

haku> /status
门状态:      open          ← 开门 → 算法自动发 CLOSE_DOOR

haku> /sim input door_close_done 1    ← 模拟门关到位
已模拟 door_close_done (I7.5) = 1

haku> /status
门状态:      closed
待处理召唤:  []            ← 任务完成，已清理

haku> /quit
再见
```

### 实机模式

```bash
$ python3 -m core    # 不带 --simulate，连 IO2HTTP 192.168.1.201

haku> /status        # 看到的是真实 IO 状态（光幕、超重、门锁等）
haku> /call 5        # 内召 5 楼，DB11 指示灯应亮 + 接触器动作
haku> /debug on      # 观察每个 tick 输出

haku> /algo set simple_internal_call   # 验证算法切换不影响运行
haku> /reload                          # 改完 config 后重载
```