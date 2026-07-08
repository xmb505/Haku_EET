---
kind: design
name: PassengerQueue 独立于算法 pending_calls 的乘客请求管理
source: session
category: adr
---

# PassengerQueue 独立于算法 pending_calls 的乘客请求管理

_来源：9e67680 → 2cc14e5 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
评审发现 passenger.py 中实现了独立的 PassengerQueue，拥有 collect→compile→consume 三步工作流和 discard/keep 两种模式，与 algorithm 层的 pending_calls 完全隔离。这是从「电梯=玩家」理念自然衍生出的设计：乘客交互逻辑不应污染核心调度算法的数据结构。

## 决策驱动
- 算法层与用户交互层解耦
- 支持不同楼层策略（discard vs keep）
- 开门期间缓存请求的窗口期管理

## 备选方案
- **乘客请求直接写入 algorithm.pending_calls** _（已否决）_ — 优点：实现简单，减少数据结构；缺点：混淆用户交互与调度算法的职责边界；无法支持开门缓存窗口期的需求
- **独立的 PassengerQueue 配合三步工作流** — 优点：算法层保持纯净；支持 collect/compile/consume 生命周期；discard/keep 模式可配置

## 决策
在 SPEC.md §2.1 新增 PassengerQueue 子章节，正式确认乘客请求由左脑模块通过独立的 PassengerQueue 管理，采用 collect（开门期间缓存）→ compile（关门后生成路线）→ consume（next + mark_served）三步工作流，与 algorithm.pending_calls 完全隔离。

## 影响
算法层（SimpleInternalCall 等）只处理来自算法侧的内召，不感知外召/内召按钮事件；passenger_flow 扩展只能通过 PassengerQueue 接口接入，不会污染核心调度逻辑。