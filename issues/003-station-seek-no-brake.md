# 003 — 站点吸附慢速缺刹车

## 严重程度：低

## 位置
`core/executor.py:738-740` (`_level_seek_check`)

## 问题
```python
await self.motor.release_brakes()
await self.motor.start(high_speed=False, direction=correct_dir)
# ⚠️ 没有 set_speed(high_speed=False) → 只切换了低速接触器，没刹车
```

对比 INIT 反冲（line 395）的正确做法：
```python
await self.motor.release_brakes()
await self.motor.start(high_speed=False, direction=reverse_dir)
await self.motor.set_speed(high_speed=False)  # ← 叠加 slow_brake
```

`motor.start()` 只切接触器，刹车由 `set_speed()` 或 `motor.hold_stop()` 单独管理。站点吸附走漏了刹车，导致反冲速度偏快。

## 后果
- 站点吸附无刹，反冲动量积累 → **冲回站台**（overshoot），反而制造新的漂移
- 慢速 + 无刹 = 惯性过大 → 反复过冲触发循环修正 → 不稳定震颤
- 比赛环境下可能因修正过冲进入 wrong_floor 或撞限位
