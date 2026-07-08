---
kind: configuration_system
name: 基于 YAML 的静态配置与热重载系统
category: configuration_system
scope:
    - '**'
source_files:
    - config/config.yaml
    - config/io_config.yaml
    - config/display_config.yaml
    - config/ui_config.yaml
    - core/__main__.py
    - core/app.py
    - core/io_mapper.py
    - core/display.py
---

## 1. 系统与工具

- **格式**：全部使用 YAML（`config.yaml`、`io_config.yaml`、`display_config.yaml`、`ui_config.yaml`），通过 `yaml.safe_load` 解析。
- **加载入口**：CLI 参数 `--config` / `--io-config` / `--display-config` 指定路径，默认位于 `config/` 目录；`core/app.py::App.__init__` 负责读取并装配。
- **热重载**：提供 `/reload` REPL 命令 → `App.reload()`，运行时重新加载主配置、IO 映射、数码管编码，并同步到运行中的 Executor/UI。

## 2. 核心文件与职责

| 文件 | 职责 |
|---|---|
| `config/config.yaml` | 运行时开关（IO 地址、楼层范围、轿厢列表、算法名、日志级别等） |
| `config/io_config.yaml` | PLC DB 地址 ↔ 逻辑信号名映射（输入/输出，按轿厢分组） |
| `config/display_config.yaml` | 7 段数码管笔画定义、字符→笔画映射、楼层→字符映射 |
| `config/ui_config.yaml` | UI/乘客交互参数（队列模式、关门延时、指示灯闪烁间隔） |
| `core/__main__.py` | CLI 参数解析，把 config 路径传给 App |
| `core/app.py` | 统一装配器：读 `config.yaml`，构造 IOClient/IOMapper/DisplayEncoder/Algorithm，暴露 `reload()` |
| `core/io_mapper.py` | 加载 `io_config.yaml`，维护 DB↔I 地址换算表与反向索引 |
| `core/display.py` | 加载 `display_config.yaml`，提供 `show_number/show_glyph/clear_display` 等高层 API |

## 3. 架构与约定

- **分层解耦**：`App` 只持有 `dict[str, Any]` 形式的配置，不直接依赖具体字段结构；各组件各自加载自己的 YAML 文件，避免单点膨胀。
- **地址映射隔离**：所有硬件地址（DBx.DBXy.z）仅出现在 `io_config.yaml` 中，业务层通过 `IOMapper.addr_input()/addr_output()` 访问，支持 `db_to_i_offset` 做 I 区偏移。
- **显示编码可插拔**：数码管笔画与楼层显示规则完全由 `display_config.yaml` 驱动，新增字符只需改配置 + `/reload`。
- **UI 参数独立文件**：`ui_config.yaml` 与主配置分离，便于现场调整交互行为而不影响控制逻辑。
- **热重载边界**：`reload()` 会重读三个 YAML 并调用各组件的 `reload()`，但 `car_ids` 在启动时确定，不支持动态增删轿厢。

## 4. 开发者应遵循的规则

1. **新增配置项优先放入对应 YAML 文件**，不要硬编码在 Python 里。
2. **修改 IO 地址只动 `io_config.yaml`**，并通过 `IOMapper` 访问，禁止直接写死 DB 地址字符串。
3. **需要运行时可调的参数**放在 `config.yaml` 或 `ui_config.yaml`，并在 `App.reload()` 中补充同步逻辑。
4. **新增数码管字符**只在 `display_config.yaml` 的 `glyphs` 和 `floor_display` 中添加，确保新字符引用的笔画已在 `segments` 中声明。
5. **不要在代码中拼接 DB 地址**，一律通过 `mapper.addr_input()/addr_output()` 获取，保证偏移变化时无处遗漏。
6. **对敏感信息（如 IP、端口）**当前仍写在 YAML 中，若后续引入环境变量，应在 `App._load_config` 中做覆盖，保持向后兼容。