# 001 — 多站路由在算法层缺失（功能回归）

## 严重程度：高

## 位置
`core/algorithm.py:73-100` (`SimpleInternalCall.decide`)

## 问题
删除 executor 中的 `effective_stop`（多站停靠逻辑）后，算法层的对应逻辑没有补回。

当前算法选 target 时是 FIFO `pending[0]` 直飞远端站：
```
pending=[3, 5, 7], pos=1 → target=3 (pending[0]) — 好
pending=[7, 3, 5], pos=1 → target=7 — 跳过 3、5
```

## 修复
在算法 `decide` 里根据车方向，从 pending 中找同方向最近的站作为 target（类似 `above[0]` / `below[0]`）。

## 影响
- 乘客上错顺序楼层会被跳过
- 顺路抢客的中间站不起作用
