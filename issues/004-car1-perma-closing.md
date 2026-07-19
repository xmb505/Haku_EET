# 004 — car1 永久卡 CLOSING（CLOSE_DOOR done 后门态不恢复）

## 严重程度：高

## 位置
`core/executor.py` CLOSE_DOOR 动作链 + car1 日志

## 症状

```
21:58:40.452  [exec] car1 CLOSE_DOOR done: result=done    ← 正常完成
21:58:40.452  [door] car1 _on_close_event: door_close_done=1, done  ← 重复 12 次！
21:58:40.452  之后 — car1 从日志中完全消失，直到文件末尾（22:10）
              · car1: state=ready pos=5 dir=idle door=closing  ← 每 10 秒重复
22:10:06.277  其他车(car3) CLOSE_DOOR timeout  →  PLC 整体连接还在
```

## 三个可疑点

### 1. `_on_close_event` 重复触发 12 次（listener 泄露）

同一个 `door_close_done=1` 事件被 12 个重复 listener 触发了 12 次。说明 `door.close()` 每次调用 `_remove_listeners()` + `io.add_listener()` 时，没清干净旧的 listener，导致叠加。12 次 `_done.set()` + `_remove_listeners()` 是幂等的，但 listener 泄露本身可能导致后续 door 操作异常。

### 2. car1 在 CLOSE_DOOR done 后彻底沉默

21:58:40.452 之后直到日志末尾 22:10+，**完全没有 car1 的 IO 事件**。其他车（car2/3/4/6）正常收发 IO。同一时刻：
- car6: level_down = 1，正常运行
- car3: 经过 L5，到站
- car4: 关门成功，正常运行

说明不是整体通信中断，而是 **car1 的 IO 通道独瘫**。原因可能是 listener 泄露导致 IOClient 的内部 listener 列表混乱，漏掉了 car1 的事件。

### 3. door=closing 持续 10+ 分钟不变

CLOSE_DOOR done 本应写 `car.door_state = DoorState.CLOSED`。如果执行了这一行，后续 close_cron 检查 `door==CLOSED` 会跳过，不会推 CLOSE_DOOR。但状态一直卡 `closing`。

有两种可能：
- `door_state = CLOSED` 那一行被异常跳过（如 listener 泄露导致异常路径）
- 状态确实写成了 CLOSED，但后续 open_cron 推了 OPEN_DOOR 失败（静默），然后 close_cron 推了 CLOSE_DOOR，但 `_start_action` 崩溃静默

## 需进一步排查

- `controllers.py` 的 `_remove_listeners()` + `io.add_listener()` 是否存在 listener 泄露 —— 特别是重复调用 close() 的场景
- 为什么 car1 的 IO 事件通道完全沉默（可能需要 IOClient 日志确认 listener 数量变化）
- CLOSE_DOOR done 后 door_state 到底是被写了 CLOSED 还是没写（需要 `_start_action` 的完整执行路径日志）
