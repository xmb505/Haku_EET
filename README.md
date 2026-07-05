# Haku_EET —— 西门子杯电梯控制离散算法

> 用 Python + asyncio 实现的电梯控制程序，模仿 MC 服务端的本地 REPL 风格，把电梯当作"玩家"驱动。

## 设计原则

1. **电梯 = 玩家**（`core/player.py`）：只保留现实状态（楼层 / 方向 / 门 / 故障），不掺杂游戏化包装
2. **算法 → 硬件严格分层**：
   - **算法层**只看到 `Car` + `Action`，完全不知道 IO 地址存在
   - **硬件层**（执行器 FSM）只看到 `Action`，负责把动作展开为具体 IO 操作
   - 中间通过 `ActionQueue` 异步通信
3. **7 段数码管编码独立 config**：比赛现场临时改编码或 10 楼显示规则只动 `config/display_config.yaml`
4. **点位表 → IO 映射自动化**：`tools/gen_io_config.py` 解析 `点位表.md` 生成 `config/io_config.yaml`，点位表改了重跑脚本即可

---

## 三层架构

```
┌────────────────────────────────────┐
│ 算法层（高层）                        │
│ ─ 看 Car + Calls → 输出 Action 列表 │
│ ─ 不知道 Q/I/M 是什么                 │
└────────────────┬───────────────────┘
                 │ push Action
                 ↓
┌────────────────────────────────────┐
│ 硬件层（执行器 FSM）                  │
│ ─ pop Action → 展开为 IO 序列       │
│ ─ 等传感器确认 → 标记 done → 取下一条│
└────────────────┬───────────────────┘
                 │ 实际 IO
                 ↓
┌────────────────────────────────────┐
│ 物理层（io_client + io_mapper）       │
└────────────────────────────────────┘
```

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
├── README.md                  # 本文件
├── COMMAND_MANUAL.md          # REPL 命令手册
├── requirements.txt
├── config/
│   ├── config.yaml            # 主配置（IO2HTTP 地址、楼层、算法、初始化方向）
│   ├── io_config.yaml         # IO 映射（自动生成）
│   └── display_config.yaml    # 7 段数码管编码
├── tools/
│   └── gen_io_config.py       # 点位表 → io_config.yaml 解析脚本
├── core/                      # 主程序
│   ├── player.py              # 玩家抽象（Car / Direction / DoorState / FaultFlags）
│   ├── actions.py             # 动作枚举 + ActionQueue
│   ├── algorithm.py           # 高层算法（基类 + 首版 SimpleInternalCall）
│   ├── executor.py            # 硬件层 FSM（动作→IO + 等传感器）
│   ├── display.py             # 7 段数码管查表
│   ├── io_mapper.py           # DB 地址 ↔ I 地址 + 逻辑信号名查表
│   ├── io_client.py           # 异步 IO2HTTP 客户端（aiohttp + websockets）
│   ├── app.py                 # 装配 + 主协调循环
│   ├── console.py             # REPL 控制台
│   └── __main__.py            # CLI 入口
└── tests/                     # pytest 单测
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
pytest tests/                        # 跑全部单测
pytest tests/test_executor.py -v     # 单跑 executor
```

测试覆盖：
- `test_player.py`：玩家抽象（10 个）
- `test_actions.py`：Action / ActionQueue（10 个）
- `test_algorithm.py`：高层算法决策（13 个）
- `test_display.py`：7 段编码查表（14 个）
- `test_io_mapper.py`：DB↔I 偏移 + 信号名查表（22 个）
- `test_io_client.py`：异步 IO 客户端（10 个）
- `test_executor.py`：硬件层 FSM（7 个）
- `test_app.py`：集成测试（9 个）

---

## 不在本期范围（明确剔除）

- ❌ 6 部电梯群控（首版 1 部）
- ❌ 外召分派（首版只做内召）
- ❌ 集选/节能算法（首版 SimpleInternalCall）
- ❌ VVVF 变频曲线（点位表是双速 + 3 级减速）
- ❌ 远程 Web 控制台（已选本地 REPL）

---

## 已知约束

- **点位表是 IO 真相来源**：`tools/gen_io_config.py` 是它的消费端，改点位表后重跑脚本
- **支持最多 6 部电梯**：`config.yaml` 里 `elevator.car_ids` 决定实例化哪些轿厢（默认全部 6 部），改完需重启程序生效
- **首版算法只响应内召**：外召按钮点位已映射但未启用（算法直接忽略 hall_call）
- **IO2HTTP 必须先于 Haku_EET 启动**：否则 HTTP POST 会失败

---

## 命令手册

完整 REPL 命令参见 [COMMAND_MANUAL.md](./COMMAND_MANUAL.md)。