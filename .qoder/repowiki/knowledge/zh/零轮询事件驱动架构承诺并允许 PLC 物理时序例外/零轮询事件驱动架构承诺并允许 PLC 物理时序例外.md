---
kind: design
name: 零轮询事件驱动架构承诺并允许 PLC 物理时序例外
source: session
category: adr
---

# 零轮询事件驱动架构承诺并允许 PLC 物理时序例外

_来源：9e67680 → 2cc14e5 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
项目明确禁止 time.sleep 和 while: sleep，要求纯事件驱动（cron.py 是唯一持有延时意图的地方）。但在 executor.py:_arrive_and_brake 中发现一处 await asyncio.sleep(0.1)，原因是 PLC 物理时序要求在 hold_stop 和 _complete_action 之间必须有 100ms 死区以防止过冲。这暴露了「零 sleep」哲学与真实硬件约束之间的冲突。

## 决策驱动
- 纯事件驱动的确定性与时序可预测性
- PLC 硬件物理时序不可绕过
- 避免引入 cron 回调带来的额外复杂度

## 备选方案
- **彻底消除所有 sleep，用 EventRule 或 Future 替代** _（已否决）_ — 优点：完全遵守零轮询原则；缺点：需要额外的 PLC 反馈信号（如霍尔传感器/刹车到位信号）才能判断停稳，比赛现场未必提供
- **保留 100ms sleep 作为工程哲学例外，但明确标注前提条件** — 优点：无需额外硬件改动即可工作；通过文档约定限制例外范围

## 决策
在 SPEC.md §7.6 新增「工程哲学例外」子章节，正式承认 brake-before-stop 100ms wait 是零 sleep 哲学的唯一例外，条件是：① 不允许改成 CronJob；② 只有当实机复现过冲 bug 且有 PLC 反馈信号时才可删除该 sleep。同时在 HANDOVER.md 赛前 checklist 中标注此例外需现场验证。

## 影响
零 sleep 原则仍是默认规则，但有了明确的例外通道；未来若硬件增加到位反馈信号，可以安全移除该 sleep 而不破坏架构一致性。