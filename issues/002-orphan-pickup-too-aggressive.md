# 002 — brain_tick orphan pickup 回收条件过强

## 严重程度：中

## 位置
`core/passenger.py:1402` (`_try_brain_tick` orphan 回收段)

## 问题
```python
if floor not in my_pending or (
        car.target_floor is not None and car.target_floor != floor):
```

`car.target_floor != floor` 几乎总是 True，导致中间站（pending 里的非 target 楼层）全被误判为 orphan 回收。

例：车 target=10，pending=[5, 7, 10]。此时 L5、L7 的 pickup 会被错误回收。

## 修复
去掉 `car.target_floor != floor` 分支，只留 `floor not in my_pending`。

## 影响
大脑心跳每 2 秒回收大量合法中间站的 pickup，被回收的外呼重新进入 `_pending_hall_calls` 等下一轮派车；不致命但产生循环回收的日志噪音 + 增加调度延迟。
