# Haku_EET 比赛现场实战教程

> 面向比赛现场操作员的实战教程。每一步都给出 REPL 命令示例，跟着做即可。
>
> 完整命令参数说明见 [COMMAND_MANUAL.md](./COMMAND_MANUAL.md)，本文不重复。

---

## 目录

- [§1 快速启动](#1-快速启动)
- [§2 初始化流程](#2-初始化流程)
- [§3 日常操作](#3-日常操作)
- [§4 用户模式（usermode）完整流程](#4-用户模式usermode完整流程)
- [§5 多轿厢操作](#5-多轿厢操作)
- [§6 调试与诊断](#6-调试与诊断)
- [§7 比赛现场配置调整](#7-比赛现场配置调整)
- [§8 常见问题与排障](#8-常见问题与排障)
- [§9 赛前硬件验证 Checklist](#9-赛前硬件验证-checklist)

---

## §1 快速启动

> **什么时候读这一节：** 比赛开始，从零把电梯跑起来。

### 第一步：启动 IO2HTTP 守护进程

⚠️ **IO2HTTP 必须先于 Haku_EET 启动**，否则所有 IO 通信失败。

```bash
cd ~/GPIOSERVER/IO2HTTP
python3 daemon_gpio.py               # 生产模式（连接真实 PLC）
# 或
python3 daemon_gpio.py --simulate    # 模拟模式（无硬件调试）
```

### 第二步：安装依赖（首次运行）

```bash
cd /home/xmb505/Haku_EET
pip install -r requirements.txt
```

### 第三步：生成 IO 映射（首次运行或点位表变更后）

```bash
python3 tools/gen_io_config.py
```

### 第四步：启动 Haku_EET

```bash
# 模拟模式（无需 PLC 硬件）
python3 -m core --simulate

# 实机模式（连接 IO2HTTP，默认地址在 config.yaml 配置）
python3 -m core
```

启动后看到 REPL 提示符即表示就绪。输入 `/help` 查看命令列表。

### 快速参考

| 步骤 | 命令 | 说明 |
|------|------|------|
| 1. 启 IO2HTTP | `cd ~/GPIOSERVER/IO2HTTP && python3 daemon_gpio.py` | 必须先于 Haku_EET |
| 2. 装依赖 | `pip install -r requirements.txt` | 首次运行 |
| 3. 生成 IO | `python3 tools/gen_io_config.py` | 首次 / 点位表变更后 |
| 4. 启动程序 | `python3 -m core` 或 `python3 -m core --simulate` | 实机 / 模拟 |

---

## §2 初始化流程

> **什么时候读这一节：** 程序启动后，电梯第一次运行前必须初始化。

### 为什么需要 init

程序启动时不知道电梯在几楼（IO 缓存为空）。初始化让电梯跑向限位开关，确认当前位置，之后才能精确运动。

⚠️ **IO 缓存为空时禁止 INITIALIZE**——必须先操作一个按钮（如轿内选层按钮），触发 IO2HTTP 推送完整 I 区 bitmap，然后再执行 init。

### 怎么 init

```
haku> /car 1 init
```

默认使用 `config.yaml` 中的 `initialization_direction`（默认 `down`），也可以手动指定：

```
haku> /car 1 init down          # 下行到下基站 → 反向到 L1
haku> /car 1 init up 3          # 上行到上基站 → 反向计数到 3 楼
haku> /car 1 init down 5        # 下行到下基站 → 反向计数到 5 楼
```

### init 过程中会看到什么

```
1 号梯初始化的完整流程：
  全速朝方向跑 → 触 1 限位 → 反向 → 逐层完美平层计数 → 停在目标楼层
```

完成后状态变为 `ready`，位置停在目标楼层（默认为 L1）。

### 验证 init 成功

```
haku> /car 1 status
```

确认：
- **状态：** `ready`
- **当前位置：** 目标楼层（如 `L1`）
- **方向：** `idle`

⚠️ init 期间**不接受任何其他命令**（锁所有 Action），需等 init 完成后再操作。

---

## §3 日常操作

> **什么时候读这一节：** 电梯已 init 完成，需要执行内召、外召、开关门等操作。

### 内召（call）

让电梯到指定楼层：

```
haku> /car 1 call 5             # 1 号梯去 5 楼
haku> /car 1 call 1             # 1 号梯回 1 楼
```

⚠️ call 只负责运动，**不控制开关门**。门控由独立子系统管理。

### 外召

外召按钮需要**用户模式（usermode）启用后才生效**。详见 [§4](#4-用户模式usermode完整流程)。

### 开关门

```
haku> /door 1 open              # 1 号梯开门
haku> /door 1 close             # 1 号梯关门
haku> /door 1 open force        # 强制开门（即使运动中）
```

⚠️ 运行中即使 `force` 也拒绝——等电梯停稳再操作。

### 查看状态

```
haku> /car 1 status
```

输出示例：

```
轿厢 ID:     1
状态:        ready
当前位置:    L5
方向:        idle
门状态:      open
目标楼层:    -
故障:        无
```

### 更改目的地（运行中）

```
haku> /car 1 change 3           # 运行中改目标为 3 楼（仅可缩短行程）
```

### 救火模式

```
haku> /car 1 fireman 5          # 救火到 5 楼（自动停靠最近平层点后倒车）
```

### 快速参考

| 操作 | 命令 | 备注 |
|------|------|------|
| 内召 | `/car 1 call 5` | 到目标层 |
| 开门 | `/door 1 open` | 需停稳 |
| 关门 | `/door 1 close` | 非阻塞 |
| 查状态 | `/car 1 status` | 随时可用 |
| 改目标 | `/car 1 change 3` | 运行中可用 |
| 救火 | `/car 1 fireman 5` | 自动倒车 |

---

## §4 用户模式（usermode）完整流程

> **什么时候读这一节：** 比赛的正常运营模式。外召按钮、自动开关门等乘客交互功能需要 usermode 才能生效。

### 前提条件

⚠️ **所有轿厢必须先完成 init**，否则 usermode 无法开启。

### 开启 usermode

```
haku> /usermode true
```

开启后：
- ready 信号置 1（告诉 PLC 系统就绪）
- 外召按钮生效
- 内召按钮自动触发调度
- 自动开关门逻辑启用

### 关闭 usermode

```
haku> /usermode false
```

### 查看当前状态

```
haku> /usermode
```

输出：`usermode(用户模式): 启用` 或 `禁用`。

### 单车测试模式（partial）

如果只想测试单部车，不必全部 init：

```
haku> /usermode partial true    # 只对已就绪的车启用
haku> /usermode partial false   # 关闭
```

### 比赛正常运营完整流程

```
haku> /car all init down        # ① 全部车初始化
haku> /usermode true            # ② 开启用户模式
                                # ③ 等待乘客按按钮，系统自动调度
haku> /usermode false           # ④ 比赛结束，关闭用户模式
haku> /quit                     # ⑤ 退出程序
```

---

## §5 多轿厢操作

> **什么时候读这一节：** 比赛需要同时控制多部电梯。

### 指定单部车

```
haku> /car 1 init down          # 只初始化 1 号梯
haku> /car 3 call 7             # 只让 3 号梯去 7 楼
```

### 操作所有车（all）

```
haku> /car all init down        # 全部车初始化（默认下行到 L1）
haku> /car all init down 1      # 全部车初始化，停在 L1
haku> /car all call 1           # 全部车回 1 楼
haku> /car all stop             # 全部车紧急停止
```

### 操作指定多辆车（逗号列表）

```
haku> /car 1,2,3 init down      # 只初始化 1、2、3 号梯
haku> /car 1,2 call 5           # 1、2 号梯去 5 楼
```

### 批量 call（不同车去不同楼层）

```
haku> /car all call 1,4,7,2,5,8    # 6 部车分别去 1/4/7/2/5/8 楼
```

### 批量初始化时的方向列表

```
haku> /car all init down,down,down,up,up,up    # 前 3 部下行，后 3 部上行
```

### UI 批量操作

```
haku> /buttonui in 1,2,3 1-10 true    # 1-3 号梯所有内召灯亮
haku> /buttonui in all all false      # 所有轿厢内召灯全灭
haku> /buttonui out 1-9 up true       # 外召 1-9 层上行灯全亮
haku> /ui all warn true               # 所有车亮故障灯
```

### 快速参考

| 操作 | 命令 | 说明 |
|------|------|------|
| 全部 init | `/car all init down` | 比赛开始首选 |
| 全部回 1 楼 | `/car all call 1` | 复位 |
| 全部停止 | `/car all stop` | 紧急停止 |
| 指定多车 | `/car 1,2,3 call 5` | 逗号分隔 |
| 批量 call | `/car all call 1,4,7,2,5,8` | 一一对应 |

---

## §6 调试与诊断

> **什么时候读这一节：** 电梯行为异常，需要观察内部状态排查问题。

### 调试日志

```
haku> /debug on                 # 开启 tick 输出 + executor 状态变化
haku> /debug off                # 关闭
```

### 精细监视（按需 toggle）

| 监视项 | 命令 | 用途 |
|--------|------|------|
| 平层信号 | `/debug show pass_floor` | 观察每次经过楼层 |
| 输入变化 | `/debug show input_change` | 观察 I 点信号变化 |
| WS 连接 | `/debug show websocket_connect_status` | 观察 WebSocket 状态 |
| 执行日志 | `/debug show exec_trace` | 观察 executor 执行过程 |
| 速度档位 | `/debug show elevator_speed` | 观察高速/减速/刹车 |
| 平层检测 | `/debug show level_check` | 观察 level 信号翻转 |
| 站点吸附 | `/debug show station_seek` | 观察吸附状态/反冲 |
| 门状态 | `/debug show door_status` | 观察门动作完成 |
| UI 事件 | `/debug show ui_listener` | 观察按钮/外召/过载 |
| 人类预测 | `/debug show human_presence` | 观察 -1/0/1 三态 |
| 门事件 | `/debug show door_event` | 观察 door done/relay/lock |
| UI 输出 | `/debug show ui_light_listener` | 观察轿内灯/外召灯 |

每个监视命令再执行一次即关闭（toggle）。

### 手动控制模式

进入手动模式可直接用键盘控制电梯运动：

```
haku> /car 1 manual
```

手动模式按键：

| 按键 | 功能 |
|------|------|
| ↑ ↓ ← → | 上下行（低速） |
| Shift+↑ ↓ | 上下行（高速） |
| 空格 | 刹车（按当前档位） |
| 0 | 释放所有刹车 |
| 1-6 | 设置刹车档位（6=全刹） |
| ESC / q | 退出手动控制 |

⚠️ 手动模式下 executor 暂停、2 限位保护关闭，可以撞限位看 PLC 反应。退出后自动恢复保护。

切回自动模式：

```
haku> /car 1 auto               # 释放刹车、停电机、算法接管
```

### 站点吸附

到站停车后自动监测平层，偏离时全速反冲回完美平层：

```
haku> /module station_seek on   # 启用
haku> /module station_seek off  # 关闭
```

查看模块状态：

```
haku> /module                   # 列出所有模块及开关状态
```

### 清空输出

```
haku> /clear                    # 所有输出位置零（不含 ready 信号）
```

---

## §7 比赛现场配置调整

> **什么时候读这一节：** 比赛现场环境变化，需要调整程序参数。

所有配置修改后执行 `/reload` 即可热加载，**无需重启程序**（除 `car_ids` 外）。

### 改初始化方向

编辑 `config/config.yaml`：

```yaml
elevator:
  initialization_direction: up      # 改 down 为 up（或反之）
```

```
haku> /reload
```

### 改 10 楼显示

编辑 `config/display_config.yaml`：

```yaml
floor_display:
  10: 'A'                           # 改成 A、blank 或任何已定义字符
```

```
haku> /reload
```

### 改 tick 写合并频率

编辑 `config/config.yaml`：

```yaml
io2http:
  tick_interval_ms: 50              # 增大减少 HTTP 请求数，减小降低 IO 延迟
```

```
haku> /reload
```

### 改低速刹车档位

运行时直接修改，并自动落盘：

```
haku> /settings slow_brake 3        # 设置低速阶段叠加刹车档位（0-6）
haku> /settings                     # 查看当前值
```

### 改轿厢数量

⚠️ `car_ids` **不支持 /reload 热加载**，必须重启程序。

编辑 `config/config.yaml`：

```yaml
elevator:
  car_ids: [1, 2, 3]               # 只跑 3 部
```

然后重启：

```bash
python3 -m core
```

### 改 IO2HTTP 地址（比赛现场必做）

编辑 `config/config.yaml`：

```yaml
io2http:
  http_url: http://192.168.1.201:8080/gpio    # 改成实际 PLC 地址
  ws_url: ws://192.168.1.201:8081/
```

```
haku> /reload
```

### 快速参考

| 要改什么 | 改哪个文件 | 热加载 |
|----------|-----------|--------|
| init 方向 | `config.yaml` → `initialization_direction` | ✅ `/reload` |
| 10 楼显示 | `display_config.yaml` → `floor_display.10` | ✅ `/reload` |
| tick 频率 | `config.yaml` → `tick_interval_ms` | ✅ `/reload` |
| 刹车档位 | REPL `/settings slow_brake <N>` | ✅ 自动落盘 |
| 轿厢数量 | `config.yaml` → `car_ids` | ❌ 需重启 |
| IO2HTTP 地址 | `config.yaml` → `io2http` | ✅ `/reload` |
| REPL 提示符 | `config.yaml` → `console.prompt` | ✅ `/reload` |

---

## §8 常见问题与排障

> **什么时候读这一节：** 电梯行为不符合预期时查阅。

### 电梯不关门

**可能原因：**
- 光幕信号持续触发（被遮挡）→ cron 无限推迟关门
- 开门按钮卡住 → 每次触发都取消关门闹钟

**排查：**

```
haku> /debug show door_event        # 观察门事件
haku> /debug show input_change      # 观察光幕信号是否持续触发
```

### init 失败 / 无法 init

**可能原因 1：IO 缓存为空**

启动后直接 init 会失败，因为 IO 缓存为空时禁止 INITIALIZE。

**解决：** 先操作一个按钮（如轿内选层按钮），触发 IO2HTTP 推送 bitmap，再 init。

**可能原因 2：限位开关异常**

电梯全速跑方向但一直触不到限位。

**排查：**

```
haku> /debug show input_change      # 看限位信号是否触发
```

对照 [IO_UI.md](./IO_UI.md) 确认限位信号名。

### IO 连接失败

**症状：** 启动后 HTTP 请求报错、WS 连接不上。

**排查：**
1. 确认 IO2HTTP 已启动（`python3 daemon_gpio.py`）
2. 确认 `config.yaml` 中的地址与实际一致
3. 检查 WS 连接状态：

```
haku> /debug show websocket_connect_status
```

### usermode 无法开启

**症状：** 执行 `/usermode true` 失败。

**原因：** 不是所有轿厢都完成了 init。

**解决：**

```
haku> /car all init down            # 确保全部 init
haku> /usermode true                # 再试
```

或先用 partial 模式测试：

```
haku> /usermode partial true        # 只对已就绪的车启用
```

### 电梯过冲 / 停不准

**排查：**

```
haku> /settings slow_brake 4        # 增大低速刹车档位（0-6）
haku> /debug show elevator_speed    # 观察速度档位切换时机
haku> /debug show pass_floor        # 观察过冲几层
```

如仍过冲，可启用站点吸附：

```
haku> /module station_seek on       # 到站后自动反冲回完美平层
```

### 外召按钮没反应

**原因：** usermode 未启用。

**解决：**

```
haku> /usermode true                # 启用用户模式
```

### 紧急停止

```
haku> /car 1 stop                   # 单车紧急停止
haku> /car all stop                 # 全部车紧急停止
```

---

## §9 赛前硬件验证 Checklist

> **什么时候读这一节：** 比赛现场接线完成后、正式运行前，逐条验证硬件假设。

验证方法：在 REPL 中观察 IO 信号值，对照 [IO_UI.md](./IO_UI.md) 确认。

| # | 验证项 | 预期行为 | 若不符合 |
|---|--------|----------|----------|
| 1 | **电磁刹车极性** | `brake = 0` 释放，`brake = 1` 刹死 | 反转 `set_brakes()` 内部 0/1 映射 |
| 2 | **限位开关常闭/常开** | 触发限位 → EMERGENCY_STOP | 反转限位触发逻辑 |
| 3 | **电机接触器极性** | 正逻辑（1=吸合） | 改 MotorController 内部映射 |
| 4 | **门到位信号极性** | `door_open/close_done = 1` 到位 | 反转到位判断逻辑 |
| 5 | **平层信号电平** | `level_up & level_down` 到位 = 1 | 反转平层到达判断 |
| 6 | **光幕信号电平** | `light_curtain = 1` 遮挡中 | 反转遮挡判断 |

### 验证步骤

```
haku> /debug show input_change      # 开启输入监视
haku> /car 1 manual                 # 进入手动模式
```

1. **刹车：** 手动运行 → 按空格刹车 → 确认电梯停住；按 0 释放 → 确认电梯可动
2. **限位：** 手动全速跑向上 → 触顶限位 → 确认 EMERGENCY_STOP 触发
3. **接触器：** 手动 ↑ → 确认电梯上行（方向正确）
4. **门到位：** `/door 1 open` → 确认 `door_open_done` 信号出现
5. **平层：** 手动慢速过楼层 → 确认 `level_up`/`level_down` 信号触发
6. **光幕：** 用手遮挡光幕 → 确认 `light_curtain` 信号变为 1

⚠️ 每条验证通过后记录到比赛日志。不符合预期时需修改对应代码映射后再正式运行。

---

> **更多细节：** [COMMAND_MANUAL.md](./COMMAND_MANUAL.md) | 架构设计：[HANDOVER.md](./HANDOVER.md) | 设计规格：[SPEC.md](./SPEC.md) | IO 点位：[IO_UI.md](./IO_UI.md)
