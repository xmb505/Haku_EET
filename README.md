# Haku_EET —— 西门子杯电梯控制离散算法（已归档）

> **📦 项目状态：已归档** —— CIMC 2025（西门子杯）初赛已结束，本项目不再活跃开发。
>
> 用 Python + asyncio 实现的电梯控制程序，模仿 MC 服务端的本地 REPL 风格，把电梯当作"玩家"驱动。

> **文档导航：** 设计哲学与完整规格 → [SPEC.md](./SPEC.md) | 交接入门 → [HANDOVER.md](./HANDOVER.md) | 命令手册 → [COMMAND_MANUAL.md](./COMMAND_MANUAL.md) | 点位对照 → [IO_UI.md](./IO_UI.md) | PLC IO → [PLC_IO.md](./PLC_IO.md)
>
> 本 README 是快速上手起点；架构 / 设计哲学 / 路线图以 SPEC.md 为真相来源。

## 设计哲学：游戏化编程

> **电梯是玩家，有属性有数值有状态。不考虑物理世界，面向评测编程。**

类比游戏引擎：大脑改 `Car` 属性 → 小脑检测变化自动同步到物理 IO → 脑干只做实际写入 PLC。
就像游戏里改角色 `position`，引擎自动渲染到屏幕。

### 8 条不变量

完整阐述见 [SPEC.md §1.3](./SPEC.md)：

1. **三层分离**：大脑（决策层）/ 小脑（物理层：运动+用户交互）/ 脑干（IO 层）严格分离，不允许跳层
2. **电梯 = 玩家**：`Car` 是游戏实体，有属性有数值有状态，不掺杂 IO 地址
3. **游戏属性驱动**：大脑改 `Car` 属性，小脑自动同步到物理世界
4. **算法层是调度员**：只输出"谁去哪 / 谁变更目的地"，不碰运动学
5. **事件驱动**：每一层广播自己的消息、监听别人的消息，没有 `time.sleep`、没有轮询
6. **cron 是外置耳机**：不属于任何层，通过 EventRule 做 reschedule / cancel
7. **不外设逻辑不污染调度**：用户交互模块只翻译需求，算法层只做调度
8. **点位表 = IO 真相来源**：`gen_io_config.py` 解析 `点位表.md`

### 代码级哲学（精选）

- 大脑不注册 IO 监听器——原始事件由小脑处理后转发
- UI 不是 PLC 影子——不自动绑定按钮↔LED，由上层决定
- 单一 IO 写路径——所有写操作走 `set_many`，tick 合并
- 硬件可逆性——接法相反只改 `controllers.py` 一处
- NOOP 不退出保持模式——空动作不破坏长寿命状态
- 急停同步清场——重置所有长寿命状态，防止 stale 逻辑复活

> 完整 16+ 条代码嵌入式哲学见 [SPEC.md §13](./SPEC.md#13-代码嵌入的设计哲学)。

---

## 三层架构（大脑 / 小脑 / 脑干）

完整架构图见 [SPEC.md §1](./SPEC.md)。

```
大脑（决策层）—— 只看 Car，不碰 IO 地址
  ├─ app.py          装配 + 用户交互 + 主协调
  ├─ algorithm.py    调度员：全局状态+需求 → 谁去哪
  ├─ console.py      REPL 控制台
  └─ passenger.py    乘客行为抽象（可选插件）
       ↓  修改 Car 属性 / 放入 ActionQueue
小脑（物理层）—— 双向同步 Car ↔ IO
  ├─ executor.py     运动 FSM（状态机驱动）
  ├─ controllers.py  电机/门/刹车硬件封装
  ├─ ui.py           灯/按钮同步
  └─ display.py      7 段数码管
       ↓  IO 写入
脑干（IO 抽象层）—— 纯物理传输，不触碰电梯语义
  ├─ io_client.py    WS bitmap 收 + HTTP 写合并（tick 20ms）
  ├─ io_mapper.py    DB ↔ I 地址映射
  └─ virtual_plc.py  模拟 PLC

cron.py（外置耳机）— 不属于任何层，事件驱动闹钟 + EventRule
```

一句话：**大脑想、小脑做、脑干传，各管各的，事件驱动串起来。**

---

## 技术栈

| 类别 | 选型 |
|------|------|
| 语言 | Python 3.11+ |
| 异步 | asyncio + aiohttp + websockets |
| 交互 | prompt-toolkit（REPL） |
| 配置 | PyYAML |
| 测试 | pytest + pytest-asyncio |
| 通信 | IO2HTTP（WS bitmap 读 + HTTP POST 写） |
| 前端 | 原生 HTML/CSS/JS（Win95 风格 HMI，`example_web/`） |

---

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 生成 IO 映射（首次或点位表变更后）

```bash
python3 tools/gen_io_config.py
```

### 运行

```bash
# 模拟模式（无需硬件）
python3 -m core --simulate

# 实机模式（需先启动 IO2HTTP）
python3 -m core
```

进入 REPL 后输入 `/help` 查看命令列表。

### 30 秒体验

```
haku> /car 1 init down    # 初始化（下行找基站）
haku> /car 1 call 5       # 内召 5 楼
haku> /car 1 status       # 查看状态
haku> /quit               # 退出
```

### IO2HTTP 守护进程（实机模式需要）

```bash
cd ~/GPIOSERVER/IO2HTTP
python3 daemon_gpio.py               # 生产模式
python3 daemon_gpio.py --simulate    # 模拟模式
```

默认地址在 `config/config.yaml` 的 `io2http` 段配置。

---

## 目录结构

```
Haku_EET/
├── core/                      # 核心代码
│   ├── __main__.py            # CLI 入口
│   ├── player.py              # Car 游戏实体（纯数据类）
│   ├── actions.py             # Action 枚举 + ActionQueue
│   ├── algorithm.py           # 大脑：调度员
│   ├── app.py                 # 大脑：装配 + 用户交互
│   ├── console.py             # 大脑：REPL 控制台
│   ├── passenger.py           # 大脑：乘客流程管理
│   ├── executor.py            # 小脑：运动 FSM
│   ├── controllers.py         # 小脑：电机/门/刹车
│   ├── ui.py                  # 小脑：灯/按钮同步
│   ├── display.py             # 小脑：7 段数码管
│   ├── weight_manager.py      # 小脑：称重管理
│   ├── watchdog.py            # 看门狗
│   ├── cron.py                # 外置：事件驱动闹钟
│   ├── io_client.py           # 脑干：WS + HTTP 客户端
│   ├── io_mapper.py           # 脑干：DB ↔ I 映射
│   └── virtual_plc.py         # 脑干：模拟 PLC
├── config/                    # 配置文件
│   ├── config.yaml            # 主配置
│   ├── io_config.yaml         # IO 映射（自动生成）
│   ├── display_config.yaml    # 数码管编码
│   └── ui_config.yaml         # UI 配置
├── example_web/               # Win95 风格 HMI 前端
├── tests/                     # pytest 单测
├── tools/                     # 工具脚本
├── docs/                      # 比赛官方文档
├── issues/                    # 已知问题记录
├── logs/                      # 运行日志
├── SPEC.md                    # 设计规格（真相来源）
├── HANDOVER.md                # 交接文档
├── COMMAND_MANUAL.md          # REPL 命令手册
├── IO_UI.md / PLC_IO.md       # IO 点位对照
└── requirements.txt           # Python 依赖
```

---

## 测试

```bash
pytest tests/          # 全部单测
pytest tests/ -v       # 详细输出
```

---

## 功能边界

**已实现：**
- 单梯/多梯内召调度（SimpleInternalCall）
- 完整运动 FSM（初始化、减速、平层、刹车）
- 门控制（开/关/光幕/延时）
- 司机模式、火灾逃生模式、比赛模式
- 称重检测、紧急停止、看门狗
- Win95 风格 Web HMI（`example_web/`）
- 事件驱动 cron 定时器

**明确不做：**
- 群控调度（多梯全局最优分配）
- VVVF 变频曲线（硬件是双速 + 3 级减速）
- 远程 Web 控制台（已选本地 REPL）

---

## 已知约束

- 点位表是 IO 唯一真相来源，改后需重跑 `gen_io_config.py`
- 最多支持 6 部电梯（`config.yaml` → `elevator.car_ids`）
- IO2HTTP 必须先于 Haku_EET 启动

---

## 相关文档

| 文档 | 用途 |
|------|------|
| [SPEC.md](./SPEC.md) | 设计规格真相来源 |
| [HANDOVER.md](./HANDOVER.md) | 交接入门总纲 |
| [COMMAND_MANUAL.md](./COMMAND_MANUAL.md) | REPL 命令手册 |
| [IO_UI.md](./IO_UI.md) | 输出 IO 与 UI 信号映射 |
| [PLC_IO.md](./PLC_IO.md) | PLC IO 点位表 |
| [issues/](./issues/) | 已知问题与修复记录 |

---

*CIMC 2025 西门子杯初赛参赛项目，比赛结束，归档留念。*