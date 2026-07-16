# Haku_EET 命令清单

所有命令以 `/` 开头，支持 Tab 补全。

## 轿厢操作

| 命令 | 参数 | 说明 |
|------|------|------|
| `/car <id>` | — | 切换当前选中轿厢 |
| `/car <id> init` | `[up\|down] [floor]` | 初始化轿厢（全速→触限位→反向→平层计数→停靠） |
| `/car <id> call` | `<floor>` | 内召到指定楼层 |
| `/car <id> change` | `<floor>` | 运行中途改目标楼层（仅可缩短行程） |
| `/car <id> fireman` | `<floor>` | 救火模式：自动停靠最近平层后倒车前往 |
| `/car <id> status` | — | 查看轿厢完整状态快照 |
| `/car <id> stop` | — | 紧急停止 |
| `/car <ids> init` | `[dir] [floors]` | 批量初始化（如 `/car 1,2,3 init down 1,2,3`） |
| `/car <ids> call` | `<floors>` | 批量内召（如 `/car all call 1,4,7`） |

## 门控制

| 命令 | 参数 | 说明 |
|------|------|------|
| `/door <id\|all> open` | `[force]` | 开门（force 跳过预检） |
| `/door <id\|all> close` | `[force]` | 关门（force 不等待不检测） |

## 模式切换

| 命令 | 参数 | 说明 |
|------|------|------|
| `/car <id> manual` | — | 进入手动控制（方向键操控，ESC 退出） |
| `/car <id> auto` | — | 切回自动控制 |

## 乘客模拟

| 命令 | 参数 | 说明 |
|------|------|------|
| `/usermode` | — | 显示当前用户模式状态 |
| `/usermode true` | — | 启用用户模式，激活 PassengerManager（需所有轿厢已初始化） |
| `/usermode false` | — | 关闭用户模式（ready 信号归零） |
| `/usermode partial true` | — | 仅对已就绪轿厢启用，跳过未就绪车（单步测试用） |
| `/usermode partial false` | — | 关闭 partial 模式 |

## UI 指示灯

| 命令 | 参数 | 说明 |
|------|------|------|
| `/ui <id\|all> <type>` | `[true\|false]` | 轿厢指示灯：`max`(满载) / `warn`(故障) / `fan` / `light` |
| `/buttonui out <floor> <dir>` | `[true\|false]` | 外召按钮灯：`dir` = `up`/`down`，floor 支持批量 |
| `/buttonui in <id\|all> <floor>` | `[true\|false]` | 轿内按钮 LED，floor 支持批量（`1,2,3` / `1-10` / `all`） |

> 省略 `true/false` 时自动 toggle 当前状态。

## 调试监视

| 命令 | 说明 |
|------|------|
| `/debug show` | 显示所有监视项当前状态 |
| `/debug show pass_floor` | 平层经过监视 |
| `/debug show input_change` | 输入信号变化监视 |
| `/debug show websocket_connect_status` | WebSocket 连接状态监视 |
| `/debug show exec_trace` | Executor 执行日志 |
| `/debug show elevator_speed` | 速度档位监视（高速/减速/停止） |
| `/debug show level_check` | 平层检测（level 翻转时打印所有车状态） |
| `/debug show station_seek` | 站点吸附状态监视 |
| `/debug show door_status` | 门动作完成监视 |
| `/debug show ui_listener` | UI 事件监视（按钮/外召/过载/检修） |
| `/debug show human_presence` | 人类预测状态变化监视 |
| `/debug show door_event` | 门事件监视（open/close/relay/lock/curtain） |
| `/debug show ui_light_listener` | UI 输出监视（轿内灯/外召灯/指示灯） |
| `/debug show ai_need_1` | AI 诊断（关门时打印状态快照） |

> 每个 `show` 命令再次执行即关闭（toggle）。

## 设置

| 命令 | 参数 | 说明 |
|------|------|------|
| `/settings slow_brake` | `[0-6]` | 查看/设置低速阶段叠加刹车档位（所有轿厢，落盘） |
| `/module` | — | 显示所有模块当前状态 |
| `/module station_seek` | `[true\|false]` | 站点吸附模块开关 |
| `/module queue` | `[discard\|keep]` | 乘客队列模式切换 |

## 系统管理

| 命令 | 说明 |
|------|------|
| `/clear` | 所有输出位置零（不含 ready 信号） |
| `/reload` | 热重载全部配置文件 |
| `/help` | 显示命令帮助 |
| `/quit` | 退出程序 |
