---
kind: configuration_system
name: 基于 YAML 的多文件分层配置与热重载系统
category: configuration_system
scope:
    - '**'
source_files:
    - config/config.yaml
    - config/io_config.yaml
    - config/display_config.yaml
    - config/ui_config.yaml
    - core/app.py
    - core/io_mapper.py
---

## 1. 系统概览

本项目采用**多 YAML 文件 + 运行时热重载**的配置体系，将电梯控制系统的运行参数、硬件点位映射、显示编码和 UI 行为拆分为独立配置文件，由 `core/app.py` 的 `App` 类统一装配，并通过 REPL `/reload` 命令实现零停机热更新。

## 2. 配置文件分层

| 文件 | 职责 | 可热重载 |
|------|------|----------|
| `config/config.yaml` | 主配置：IO 网关地址、楼层范围、轿厢列表、算法名、日志级别等 | ✅ |
| `config/io_config.yaml` | IO 点位映射：DB 地址 → 逻辑信号名（输入/输出/每轿厢） | ✅ |
| `config/display_config.yaml` | 7 段数码管字符编码、楼层→字符映射 | ✅ |
| `config/ui_config.yaml` | 乘客交互 UI 参数：队列模式、关门延时、指示灯闪烁间隔 | ✅ |

## 3. 加载与装配流程

- **启动时**：`App.__init__` 调用 `_load_config()` 读取 `config.yaml`，再构造 `IOMapper(io_config_path)`、`DisplayEncoder(display_config_path, ...)`，并解析 `elevator.car_ids` 为每部轿厢创建独立的 `ActionExecutor`、`UiController` 和写通道 `IOClient`。
- **热重载**：`App.reload()` 重新加载所有 YAML，同步更新 `mapper`、`display`、各 `executor` 的运行参数（初始化方向、站点吸附开关、低速刹车档位），以及 `io._tick_interval`。
- **持久化写入**：`_save_elevator_config(key, value)` 使用正则替换 `config.yaml` 中对应行，保留注释格式，仅支持 `elevator.*` 键值。

## 4. 核心组件

- **`IOMapper`**：纯数据层，负责 `io_config.yaml` 的解析与 DB↔I 地址换算，提供 `(car_id, signal) → DB 地址` 及反向索引，供 `IOClient`、`DisplayEncoder`、`App` 事件路由使用。
- **`DisplayEncoder`**：封装 `display_config.yaml` 的 glyph/floor_display 表，按轿厢生成数码管段码。
- **`PassengerManager`**（可选插件）：通过 `ui_config_path` 注入，与 `App` 解耦，缺失时不影响小脑运行。

## 5. 设计约定与约束

- **YAML 安全加载**：全部使用 `yaml.safe_load`，禁止任意对象反序列化。
- **地址合法性校验**：`IOMapper.db_to_i` / `i_to_db` 对 DB/I 地址做正则匹配与越界检查。
- **热重载边界**：`car_ids` 在启动时确定，`/reload` 不会动态增删轿厢；新增轿厢需重启程序。
- **配置来源单一**：无环境变量、命令行覆盖或远程配置中心，所有配置来自本地 YAML 文件。
- **UI 配置分离**：`ui_config.yaml` 与主配置解耦，便于比赛现场快速调整交互行为而不影响控制逻辑。

## 6. 开发者规则

1. 新增运行参数请放入 `config.yaml` 对应 section，并在 `App.reload()` 中补充同步逻辑。
2. 新增 IO 信号请在 `config/io_config.yaml` 的 `input`/`output.per_car.<car_id>` 下注册，保持命名一致。
3. 修改 `db_to_i_offset` 后需确保 PLC 侧 I 区偏移一致，否则 WebSocket 事件无法正确反查。
4. 不要直接修改内存中的 `self.config` 来持久化——应通过 `_save_elevator_config` 写回文件。
5. 新增 YAML 文件需在 `App.__init__` 和 `reload()` 中显式加载，避免遗漏热更新路径。
