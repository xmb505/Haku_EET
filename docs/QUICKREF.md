# Haku_EET 比赛现场速查卡

> 架构：大脑（调度+决策）/ 小脑（IO 同步+执行）/ 脑干（物理传输）
> 所有命令以 `/` 开头，支持 **Tab 补全** + **上下键历史**。

---

## 1. 启动速查

```bash
# ① 先启 IO2HTTP（脑干物理层，必须先于 Haku_EET）
cd ~/GPIOSERVER/IO2HTTP && python3 daemon_gpio.py

# ② 启动 Haku_EET
python3 -m core --simulate    # 模拟模式（无硬件）
python3 -m core               # 实机模式（连 IO2HTTP）
```

⚠️ 首次运行需 `pip install -r requirements.txt` + `python3 tools/gen_io_config.py`

---

## 2. 比赛日常流程

| # | 操作 | 命令 |
|---|------|------|
| 1 | 先按一个按钮触发 bitmap（脑干收到 IO） | （物理按钮） |
| 2 | 全部车初始化（小脑 executor 执行） | `/car all init down` |
| 3 | 确认 init 完成 | `/car 1 status` → 状态=ready |
| 4 | 开启用户模式（大脑开始接单调度） | `/usermode true` |
| 5 | 正常运营（大脑自动派车+小脑执行） | 等待乘客按按钮 |
| 6 | 比赛结束关用户模式 | `/usermode false` |
| 7 | 退出 | `/quit` |

---

## 3. 高频命令表

| 命令 | 作用 | 备注 |
|------|------|------|
| `/car all init down` | 全部车初始化 | **比赛第一步**，IO 非空才能执行 |
| `/car 1 init up 3` | 单车初始化到指定层 | 可选方向+楼层 |
| `/car 1 call 5` | 内召 5 楼 | 只管运动不管门 |
| `/car all call 1` | 全部车回 1 楼 | 复位用 |
| `/car 1 status` | 查看状态（含大脑队列） | 随时可用 |
| `/car all stop` | 全部车紧急停止 | 脑干层立即归零 |
| `/car 1 change 3` | 运行中改目标 | 仅可缩短行程 |
| `/car 1 fireman 5` | 救火到 5 楼 | 自动停靠+倒车 |
| `/usermode true` | 开启用户模式 | **需所有车已 init**，大脑开始接客 |
| `/usermode partial true` | 只对已就绪车启用 | 单车测试用 |
| `/usermode` | 查看当前状态 | — |
| `/door 1 open` / `close` | 开关门 | 需停稳；非阻塞 |
| `/door 1 open force` | 强制开门 | 运行中拒绝 |
| `/module station_seek true` | 启用站点吸附（小脑特性） | 到站自动反冲 |
| `/module queue discard` | 大脑队列模式：过站丢弃 | 适合高频外召 |
| `/module queue keep` | 大脑队列模式：保留 | 不丢乘客 |
| `/module` | 查看所有模块状态 | — |
| `/debug on` / `off` | 开关调试日志 | tick+executor |
| `/debug show input_change` | 监视 IO 输入变化 | toggle 开关 |
| `/debug show ui_listener` | 监视大脑接收的按钮事件 | toggle 开关 |
| `/debug show door_event` | 监视门事件 | toggle 开关 |
| `/clear` | 小脑所有输出归零 | 不含 ready |
| `/reload` | 热加载全部 config | 无需重启 |
| `/settings slow_brake 3` | 低速刹车档位(0-6) | 自动落盘 |
| `/help` | 显示帮助 | — |
| `/quit` | 退出 | Ctrl-C/D 也可 |

---

## 4. 外召派车规则（大脑调度）

1. **顺向经过** → 最高优先（运动方向=召唤方向 且途经该楼层）
2. **空闲最近** → 次优先（IDLE 状态，按距离排序）
3. **距离相同** → 取小 car_id
4. 排除：门未关 / 手动模式 / 已在接该客 / FAULT

> `/module queue discard` → 过站即丢弃未服务请求（防积压）
> `/module queue keep` → 保留所有请求直到被服务

---

## 5. 配置热调整表

| 想改什么 | 改哪个文件 | 热加载 |
|----------|-----------|--------|
| init 方向 | `config.yaml` → `initialization_direction` | `/reload` |
| IO2HTTP 地址 | `config.yaml` → `io2http.http_url / ws_url` | `/reload` |
| tick 频率 | `config.yaml` → `tick_interval_ms` | `/reload` |
| 10 楼显示 | `display_config.yaml` → `floor_display.10` | `/reload` |
| REPL 提示符 | `config.yaml` → `console.prompt` | `/reload` |
| 刹车档位 | REPL `/settings slow_brake <0-6>` | 自动落盘 |
| 轿厢数量 | `config.yaml` → `car_ids` | **需重启** |

---

## 6. 故障速查表

| 现象 | 原因 | 一句话解决 |
|------|------|-----------|
| init 提示 bitmap 为空 | 脑干未收到 IO 数据 | 先按一个轿内按钮触发 bitmap |
| init 一直跑不停 | 限位信号没触发 | `/debug show input_change` 查限位 |
| `/usermode true` 被拒 | 有车未 init（大脑拒绝） | `/car all init down` 后重试 |
| 外召按钮没反应 | usermode 未启用，大脑不接单 | `/usermode true` |
| 电梯不关门 | 光幕持续遮挡→cron 推迟关门 | `/debug show input_change` 查光幕 |
| 过冲 / 停不准 | 小脑刹车不够 | `/settings slow_brake 4` + `/module station_seek true` |
| IO 连接失败 | IO2HTTP 没启动或地址错 | 先启 daemon_gpio.py，再 `/reload` |
| FAULT 锁死 | 撞过 2 限位，小脑锁状态 | `/car N manual` 推出限位 → ESC 退出自动恢复 |

---

## 7. 赛前硬件验证 Checklist

| # | 验证项 | 预期 | 验证方法 |
|---|--------|------|----------|
| 1 | 电磁刹车极性 | `0`=释放 `1`=刹死 | 手动运行→空格刹→按 0 释放 |
| 2 | 限位开关 | 触限位→脑干触发急停 | 手动全速↑→触顶→确认停 |
| 3 | 电机接触器极性 | 正逻辑 1=吸合 | 手动↑→确认上行 |
| 4 | 门到位信号 | `done=1` 到位 | `/door 1 open`→看 `door_open_done` |
| 5 | 平层信号电平 | 双 1=完美平层 | 手动慢速过层→看 level ↑↓ |
| 6 | 光幕信号 | `1`=遮挡中 | 手挡光幕→确认变 1 |

验证步骤：`/debug show input_change` → `/car 1 manual` → 逐项测试

---

**手动模式按键：** ↑↓←→ 低速 / Shift+↑↓ 高速 / 空格 刹车 / 0-6 刹车档 / ESC 退出
