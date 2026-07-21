# 005: 检修信号（service_mode）急停与恢复

## 信号来源

PLC 每部车一个 `service_mode` 输入点（已在点位表中，当前映射为 `I6.0` 等）。

## 期望行为

### service_mode = 1（进入检修）

- **破坏性急停**：立即全刹 + 电机断电 + 门继电器归零
- 置 `CarState.FAULT`
- 清空当前动作、pending_calls、pickup、action_queue
- 亮故障灯
- 不响应任何后续 Action（INITIALIZE 除外）

### service_mode = 0（退出检修）

- 熄故障灯
- **全部重新初始化**：所有车入队 INITIALIZE（不是只恢复当前车）
- 初始化完成后恢复 READY，等待调度

## 当前实现状态

- `executor.py` 已有 `service_mode` 上升沿检测 → `_emergency_stop(reason='service_mode')`
- 下降沿（0→退出检修）目前只做了 FAULT→READY 恢复，**没有触发全车 INITIALIZE**
- 需要补充：下降沿时对所有车入队 INITIALIZE

## 实现要点

1. 下降沿检测：`service_mode` 从 1→0 时触发恢复流程
2. 恢复流程：遍历所有 car_ids，对每部车入队 `Action(INITIALIZE, floor=init_target)`
3. 防抖：PLC 信号可能抖动，考虑 100ms 去抖或只在确认稳定后触发
4. 与比赛 auto_run 的关系：检修退出后的 INITIALIZE 不依赖 auto_run 信号
