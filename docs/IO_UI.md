# 输出 IO 点位与 UI 模块映射

记录日期：2026-07-07
来源：用户提供的完整 PLC 输出 DB11 点位表

## 重要前提：开门/关门按钮灯信号不存在

**PLC 上没有独立的 `door_open_indicator` / `door_close_indicator` 按钮灯信号**。

开关门只有两个继电器输出（控制门电机，不属于 UI 模块管辖）：

| 逻辑名 | DB 地址 | 物理作用 |
|--------|---------|----------|
| `door_open_relay` | `DB11.DBX6.5` (车 1) | 开门电机继电器 |
| `door_close_relay` | `DB11.DBX6.6` (车 1) | 关门电机继电器 |

这两个继电器由 `DoorController.open()` / `DoorController.close()` 控制（executor 在执行 OPEN_DOOR / CLOSE_DOOR action 时调用），**不属于 UI 模块**。UI 模块只管"看上去是什么样的"，不管"门实际怎么动"。

## 完整输出 DB11 点位表（6 部梯）

每部车占用约 40 位，车号顺序：

```
1号梯: DBX2.2  ~ DBX7.1
2号梯: DBX7.2  ~ DBX12.1
3号梯: DBX12.2 ~ DBX17.1
4号梯: DBX17.2 ~ DBX22.1
5号梯: DBX22.2 ~ DBX27.1
6号梯: DBX27.2 ~ DBX32.1
准备就绪信号: DBX32.2
```

### 全局信号（外召指示灯）

| 楼层 | 信号 | DB 地址 |
|------|------|---------|
| 1 层上行 | `hall_indicator_up_1` | DBX0.0 |
| 2 层上行 | `hall_indicator_up_2` | DBX0.1 |
| 3 层上行 | `hall_indicator_up_3` | DBX0.2 |
| 4 层上行 | `hall_indicator_up_4` | DBX0.3 |
| 5 层上行 | `hall_indicator_up_5` | DBX0.4 |
| 6 层上行 | `hall_indicator_up_6` | DBX0.5 |
| 7 层上行 | `hall_indicator_up_7` | DBX0.6 |
| 8 层上行 | `hall_indicator_up_8` | DBX0.7 |
| 9 层上行 | `hall_indicator_up_9` | DBX1.0 |
| 2 层下行 | `hall_indicator_down_2` | DBX1.1 |
| 3 层下行 | `hall_indicator_down_3` | DBX1.2 |
| 4 层下行 | `hall_indicator_down_4` | DBX1.3 |
| 5 层下行 | `hall_indicator_down_5` | DBX1.4 |
| 6 层下行 | `hall_indicator_down_6` | DBX1.5 |
| 7 层下行 | `hall_indicator_down_7` | DBX1.6 |
| 8 层下行 | `hall_indicator_down_8` | DBX1.7 |
| 9 层下行 | `hall_indicator_down_9` | DBX2.0 |
| 10 层下行 | `hall_indicator_down_10` | DBX2.1 |

### 每部车通用布局（车 1 为例，车 N 的偏移 = `(N-1) * 5` byte）

以车 1 为基准（DBX2.2 ~ DBX7.1），每部车增量 5 byte：

| 偏移 | 信号类别 | 信号名（按车 1） | DB 地址（车 1） |
|------|----------|------------------|-----------------|
| +0.0 | 轿内按钮灯 | `cabin_button_led_1` | DBX2.2 |
| +0.1 | 轿内按钮灯 | `cabin_button_led_2` | DBX2.3 |
| +0.2 | 轿内按钮灯 | `cabin_button_led_3` | DBX2.4 |
| +0.3 | 轿内按钮灯 | `cabin_button_led_4` | DBX2.5 |
| +0.4 | 轿内按钮灯 | `cabin_button_led_5` | DBX2.6 |
| +0.5 | 轿内按钮灯 | `cabin_button_led_6` | DBX2.7 |
| +0.6 | 轿内按钮灯 | `cabin_button_led_7` | DBX3.0 |
| +0.7 | 轿内按钮灯 | `cabin_button_led_8` | DBX3.1 |
| +1.0 | 轿内按钮灯 | `cabin_button_led_9` | DBX3.2 |
| +1.1 | 轿内按钮灯 | `cabin_button_led_10` | DBX3.3 |
| +1.2 | 7 段数码管 | `segment_a` | DBX3.4 |
| +1.3 | 7 段数码管 | `segment_b` | DBX3.5 |
| +1.4 | 7 段数码管 | `segment_c` | DBX3.6 |
| +1.5 | 7 段数码管 | `segment_d` | DBX3.7 |
| +1.6 | 7 段数码管 | `segment_e` | DBX4.0 |
| +1.7 | 7 段数码管 | `segment_f` | DBX4.1 |
| +2.0 | 7 段数码管 | `segment_g` | DBX4.2 |
| +2.1 | 7 段数码管 | `segment_h` | DBX4.3 |
| +2.2 | 7 段数码管 | `segment_i` | DBX4.4 |
| +2.3 | 7 段数码管 | `segment_j` | DBX4.5 |
| +2.4 | 7 段数码管 | `segment_k` | DBX4.6 |
| +2.5 | 7 段数码管 | `segment_l` | DBX4.7 |
| +2.6 | 7 段数码管 | `segment_m` | DBX5.0 |
| +2.7 | 7 段数码管 | `segment_n` | DBX5.1 |
| +3.0 | 状态指示灯 | `up_indicator` | DBX5.2 |
| +3.1 | 状态指示灯 | `down_indicator` | DBX5.3 |
| +3.2 | 状态指示灯 | `fault_indicator` | DBX5.4 |
| +3.3 | 状态指示灯 | `light_indicator` | DBX5.5 |
| +3.4 | 状态指示灯 | `fan_indicator` | DBX5.6 |
| +3.5 | 状态指示灯 | `full_load_indicator` | DBX5.7 |
| +3.6 | 电机控制 | `motor_start` | DBX6.0 |
| +3.7 | 电机控制 | `up_contactor` | DBX6.1 |
| +4.0 | 电机控制 | `down_contactor` | DBX6.2 |
| +4.1 | 电机控制 | `high_speed_contactor` | DBX6.3 |
| +4.2 | 电机控制 | `low_speed_contactor` | DBX6.4 |
| +4.3 | 门继电器 | `door_open_relay` | DBX6.5 |
| +4.4 | 门继电器 | `door_close_relay` | DBX6.6 |
| +4.5 | 刹车 | `brake_1` | DBX6.7 |
| +4.6 | 刹车 | `brake_2` | DBX7.0 |
| +4.7 | 刹车 | `brake_3` | DBX7.1 |

### 6 部车的完整 DB 地址

| 逻辑信号 | 车 1 | 车 2 | 车 3 | 车 4 | 车 5 | 车 6 |
|----------|------|------|------|------|------|------|
| `cabin_button_led_1` | DBX2.2 | DBX7.2 | DBX12.2 | DBX17.2 | DBX22.2 | DBX27.2 |
| `segment_a` | DBX3.4 | DBX8.4 | DBX13.4 | DBX18.4 | DBX23.4 | DBX28.4 |
| `up_indicator` | DBX5.2 | DBX10.2 | DBX15.2 | DBX20.2 | DBX25.2 | DBX30.2 |
| `down_indicator` | DBX5.3 | DBX10.3 | DBX15.3 | DBX20.3 | DBX25.3 | DBX30.3 |
| `fault_indicator` | DBX5.4 | DBX10.4 | DBX15.4 | DBX20.4 | DBX25.4 | DBX30.4 |
| `light_indicator` | DBX5.5 | DBX10.5 | DBX15.5 | DBX20.5 | DBX25.5 | DBX30.5 |
| `fan_indicator` | DBX5.6 | DBX10.6 | DBX15.6 | DBX20.6 | DBX25.6 | DBX30.6 |
| `full_load_indicator` | DBX5.7 | DBX10.7 | DBX15.7 | DBX20.7 | DBX25.7 | DBX30.7 |
| `motor_start` | DBX6.0 | DBX11.0 | DBX16.0 | DBX21.0 | DBX26.0 | DBX31.0 |
| `door_open_relay` | DBX6.5 | DBX11.5 | DBX16.5 | DBX21.5 | DBX26.5 | DBX31.5 |
| `door_close_relay` | DBX6.6 | DBX11.6 | DBX16.6 | DBX21.6 | DBX26.6 | DBX31.6 |
| `brake_1` | DBX6.7 | DBX11.7 | DBX16.7 | DBX21.7 | DBX26.7 | DBX31.7 |
| `brake_2` | DBX7.0 | DBX12.0 | DBX17.0 | DBX22.0 | DBX27.0 | DBX32.0 |
| `brake_3` | DBX7.1 | DBX12.1 | DBX17.1 | DBX22.1 | DBX27.1 | DBX32.1 |

### 全局

| 信号 | DB 地址 |
|------|---------|
| `ready` | DBX32.2 |

## UI 模块信号映射

### UI 模块方法 → 实际 DB 信号

| UI 方法 | 映射的 IO 信号 | 备注 |
|---------|---------------|------|
| `set_full_load(on)` | `full_load_indicator` | LED 指示灯 |
| `set_fault(on)` | `fault_indicator` | LED 指示灯 |
| `set_light(on)` | `light_indicator` | 控制电梯内灯 |
| `set_fan(on)` | `fan_indicator` | 控制风扇 |
| `set_cabin_button_led(floor, on)` | `cabin_button_led_{floor}` | 轿内按钮 LED |

### Hall indicator

| App 方法 | 映射的 IO 信号 |
|----------|---------------|
| `set_hall_indicator(floor, 'up', on)` | `hall_indicator_up_{floor}` |
| `set_hall_indicator(floor, 'down', on)` | `hall_indicator_down_{floor}` |

## UI 模块在架构中的定位

UI 模块（`core/ui.py`）属于**小脑（物理层）**。

**设计哲学：**
- UI 模块处理灯和按钮信号（用户交互）——它把 `Car.ui` 属性同步到物理 IO 输出，也把 IO 输入的变化反映到 `Car` 属性
- 大脑（用户交互模块）决定"什么时候亮什么灯"，但**不直接操作 IO**
- 大脑修改 `Car.ui` 属性（如 `car.ui.fault = True`），UI 模块自动把变化刷到物理 IO

**关键约束：**
- 严禁直接赋值 `car.ui.fault = True` 等——那只会改逻辑状态不同步 IO
- 必须通过 `UiController` 方法（如 `app.ui[cid].set_fault(True)`）——它会同时改逻辑状态和触发 IO 写入

## 注意事项

- **开门/关门不在 UI 模块管辖**：由 `DoorController.open()/close()` 控制 `door_open_relay` / `door_close_relay`，UI 模块不参与。
- **PLC 上没有开关门按钮 LED**：想点亮的"开门/关门"视觉显示就靠电机继电器本身（硬件物理效果），不需要额外 IO。
- **floor_display 配置必须是活的**：当前 `show_number()` 不查 `display_config.floor_display`（致命缺陷 #3），修复后配置改 10→'A' 才能生效。