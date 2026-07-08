# Haku_EET —— 西门子杯电梯控制离散算法

> 用 Python + asyncio 实现的电梯控制程序，模仿 MC 服务端的本地 REPL 风格，把电梯当作"玩家"驱动。

> **文档导航：** 想理解设计哲学与完整规格 → [SPEC.md](./SPEC.md) | 想交接入门 → [HANDOVER.md](./HANDOVER.md) | 想查命令 → [COMMAND_MANUAL.md](./COMMAND_MANUAL.md) | 想对照点位 → [IO_UI.md](./IO_UI.md)
>
> 本 README 是快速上手起点；架构 / 设计哲学 / 路线图以 SPEC.md 为真相来源。

## 设计原则（8 条不变量）

完整阐述见 [SPEC.md §1.3](./SPEC.md)。以下是速览版：

1. **三层分离**：大脑（决策层）/ 小脑（物理层：运动+用户交互）/ 脑干（IO 层）严格分离，不允许跳层
2. **电梯 = 玩家**：`Car` 是游戏实体，有属性有数值有状态，不掺杂 IO 地址
3. **游戏属性驱动**：大脑改 `Car` 属性，小脑自动同步到物理世界
4. **算法层是调度员**：只输出"谁去哪 / 谁变更目的地"，不碰运动学
5. **事件驱动**：每一层广播自己的消息、监听别人的消息，没有 `time.sleep`、没有轮询
6. **cron 是外置耳机**：不属于任何层，通过 EventRule 做 reschedule / cancel
7. **不外设逻辑不污染调度**：用户交互模块只翻译需求，算法层只做调度
8. **点位表 = IO 真相来源**：`gen_io_config.py` 解析 `点位表.md`

---

## 三层架构（大脑 / 小脑 / 脑干）

完整架构图与设计哲学见 [SPEC.md §1](./SPEC.md)。

```
大脑（决策层）
  ├─ 用户交互模块 — 赋予外设逻辑，管理 cron 闹钟，修改 Car 属性
  ├─ 算法层（调度员）— 全局状态+需求 → 谁去哪
  └─ REPL 控制台 — 文本命令 → 需求
       ↓
小脑（物理层：运动+用户交互）
  ├─ executor FSM — 运动控制，更新 Car 位置/门
  ├─ UI 模块 — 灯/按钮/显示（用户交互）
  └─ controllers — 电机/门硬件封装
       ↓
       把电梯物理参数（电机、接触器、刹车、反冲等）隐藏掉，让 /car 1 call 5 成为简单高层命令
       ↓
脑干（IO 抽象层）
  ├─ io_client — WS bitmap + HTTP 写合并（含 tick flush，默认 20ms）
  ├─ io_mapper — DB <-> I 地址映射
  └─ virtual_plc — 模拟 PLC

cron（外置耳机）— 不属于任何层，定闹钟 + EventRule

> **设计哲学参考**：完整设计哲学清单（16+ 条代码嵌入式哲学）见 [SPEC.md §13](./SPEC.md#13-代码嵌入的设计哲学)。
```

游戏化编程哲学：大脑只操作 `Car` 实体属性，小脑自动把属性变化同步到物理 IO，脑干只做物理传输。没有跨层硬调用。

---

## 快速开始

### 前置：IO2HTTP 守护进程

Haku_EET 通过 [IO2HTTP](https://github.com/) 与 PLC 通信。在运行 Haku_EET 之前，确保 IO2HTTP 守护进程已启动：

```bash
cd ~/GPIOSERVER/IO2HTTP
python3 daemon_gpio.py               # 生产模式（连实 PLC）
# 或
python3 daemon_gpio.py --simulate    # 模拟模式（无硬件调试用）
```

默认监听：
- HTTP 控制：`http://192.168.1.201:8080/gpio`
- WebSocket 事件：`ws://192.168.1.201:8081/`

可在 `config/config.yaml` 的 `io2http` 段修改地址。

### 安装依赖

```bash
cd /home/xmb505/Haku_EET
pip install -r requirements.txt
```

### 生成 IO 映射（首次或点位表变更后）

```bash
python3 tools/gen_io_config.py
```

这会从 `点位表.md` 解析并生成 `config/io_config.yaml`。

### 跑起来

```bash
# 模拟模式（无需 IO2HTTP 也无需 PLC）
python3 -m core --simulate

# 实机模式（默认连 192.168.1.201 的 IO2HTTP）
python3 -m core
```

进入 REPL 后输入 `/help` 查看命令列表。

---

## 目录结构

```
Haku_EET/
├── SPEC.md                    # 设计规格真相来源（架构+契约+路线图）
├── README.md                  # 本文件
├── HANDOVER.md                # 交接文档（入门总纲）
├── COMMAND_MANUAL.md          # REPL 命令手册
├── IO_UI.md                   # 输出 IO 与 UI 模块信号映射
├── 点位表.md                   # IO 信号原始表（IO 真相来源）
├── requirements.txt
├── pytest.ini
├── config/
│   ├── config.yaml            # 主配置（IO2HTTP 地址、楼层、算法、初始化方向）
│   ├── io_config.yaml         # IO 映射（自动生成）
│   └── display_config.yaml    # 7 段数码管编码
├── tools/
│   └── gen_io_config.py       # 点位表 → io_config.yaml 解析脚本
├── core/
│   ├── player.py              # Car 游戏实体（属性/数值/状态）
│   ├── actions.py             # Action 枚举 + ActionQueue
│   ├── algorithm.py           # 大脑：算法层/调度员
│   ├── app.py                 # 大脑：装配 + 用户交互 + 主协调
│   ├── console.py             # 大脑：REPL 控制台
│   ├── passenger.py           # 大脑：乘客行为抽象（可选）
│   ├── executor.py            # 小脑：运动 FSM
│   ├── controllers.py         # 小脑：电机/门控制
│   ├── ui.py                  # 小脑：Car.ui ↔ IO 同步
│   ├── display.py             # 小脑：7 段数码管
│   ├── cron.py                # 外置：事件驱动闹钟
│   ├── io_mapper.py           # 脑干：DB ↔ I 映射
│   ├── io_client.py           # 脑干：WS + HTTP 客户端
│   ├── virtual_plc.py         # 脑干：模拟 PLC
│   ├── __init__.py
│   └── __main__.py            # CLI 入口
└── tests/                     # pytest 单测（266 个用例）
```

---

## 比赛现场快速调整

### 改初始化方向（默认下行到 1 楼 ↔ 上行到 10 楼）

编辑 `config/config.yaml`：
```yaml
elevator:
  initialization_direction: down   # 改成 up 即可
```
然后在 REPL 跑 `/reload`，无需重启程序。

### 改 10 楼显示（默认显示 0）

编辑 `config/display_config.yaml` 的 `floor_display` 段：
```yaml
floor_display:
  10: 'A'    # 改成 A、blank、或任何在 glyphs 里定义的字符
```
然后 `/reload`。

### 调试日志

```
haku> /debug on    # 打开 tick + executor 日志
haku> /debug off   # 关闭
```

---

## 测试

```bash
pytest tests/                        # 跑全部单测（266 个）
pytest tests/test_executor.py -v     # 单跑 executor
```

测试覆盖：
- `test_actions.py`：Action / ActionQueue（10 个）
- `test_algorithm.py`：高层算法决策（15 个）
- `test_app.py`：集成测试 + 控制层事件分发（50 个）
- `test_buttonui_batch.py`：UI 模块批量（30 个）
- `test_cron.py`：事件驱动定时器（7 个）
- `test_display.py`：7 段编码查表（14 个）
- `test_door.py`：`/door` 命令 + 门控制（24 个）
- `test_executor.py`：硬件层 FSM（14 个）
- `test_io_client.py`：异步 IO 客户端 + 写合并缓冲区（16 个）
- `test_io_mapper.py`：DB↔I 偏移 + 信号名查表（22 个）
- `test_manual_deadline.py`：手动模式 deadline 立即停（4 个）
- `test_player.py`：玩家抽象（10 个）
- `test_ui.py`：UI 指示灯同步（15 个）

---

## 不在本期范围（明确剔除）

- ❌ 6 部电梯群控（首版支持多轿厢但不做全局调度）
- ❌ 集选/节能算法（首版 SimpleInternalCall）
- ❌ VVVF 变频曲线（点位表是双速 + 3 级减速）
- ❌ 远程 Web 控制台（已选本地 REPL）
- ❌ 上层 passenger_flow 模块（开门后自动关门 cron、熄灯 cron、human_presence 状态迁移）——待实现，详见 [SPEC.md §11 路线图](./SPEC.md#11-待办与路线图)

---

## 已知约束

- **点位表是 IO 真相来源**：`tools/gen_io_config.py` 是它的消费端，改点位表后重跑脚本
- **支持最多 6 部电梯**：`config.yaml` 里 `elevator.car_ids` 决定实例化哪些轿厢（默认全部 6 部），改完需重启程序生效
- **首版算法只响应内召**：外召按钮点位已映射但未启用（算法直接忽略 hall_call）
- **IO2HTTP 必须先于 Haku_EET 启动**：否则 HTTP POST 会失败

---

## 命令手册

完整 REPL 命令参见 [COMMAND_MANUAL.md](./COMMAND_MANUAL.md)。