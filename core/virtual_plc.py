"""
virtual_plc.py —— 模拟 PLC（simulate 模式下的"虚拟电梯"驱动器）

设计目的:
    用户在 simulate 模式下想验证 init / 移动 / 平层 / 限位 全链路，
    但没有真实 PLC 推动接触器动作。
    VirtualPLC 监听 IOClient 的输出缓存（接触器 + 电机），
    自动驱动 position 变化，并触发对应的输入信号（平层 / 限位）。

完全独立于 executor / 算法 / action_queue —— 它只通过 IOClient 的
output_cache 和 simulate_input() 与系统交互，本质是"反向 IO 模拟"。

工作模型:
    1. 每 50ms 检查 motor + 接触器输出
    2. 若 motor+up_contactor=1: position 每 floor_travel 秒 +1
       若 motor+down_contactor=1: position 每 floor_travel 秒 -1
    3. 跨越整数层时 → 触发 level_up & level_down 脉冲（200ms 完美平层）
    4. 触到 base (top=11 / bottom=-1) → 触发 1 限位
    5. 触到 2 限位 (12 / -2) → 触发 2 限位 (紧急停止)

position 始终保持为整数（不模拟中间态），与楼层计数模型对齐。
"""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .io_client import IOClient
    from .io_mapper import IOMapper
    from .player import Car


class VirtualPLC:
    """
    模拟 PLC：在 simulate 模式下"反向"驱动 IO

    用法:
        vplc = VirtualPLC(io, mapper, car, top_base=11, bottom_base=-1)
        vplc.start()
        # ... 操作 ...
        await vplc.stop()
    """

    def __init__(
        self,
        io: 'IOClient',
        mapper: 'IOMapper',
        car: 'Car',
        car_id: int = 1,
        top_base: int = 11,
        bottom_base: int = -1,
        top_floor: int = 10,
        bottom_floor: int = 1,
        floor_travel_time: float = 0.4,  # 跑一整层需要的时间（秒）
        tick: float = 0.05,              # 主循环周期
    ) -> None:
        self.io = io
        self.mapper = mapper
        self.car = car
        self.car_id = car_id
        self.top_base = top_base
        self.bottom_base = bottom_base
        self.top_floor = top_floor
        self.bottom_floor = bottom_floor
        self.floor_travel_time = floor_travel_time
        self.tick = tick

        self._running = False
        self._task: asyncio.Task | None = None
        # 已触过哪个限位（防止重复触发）
        self._last_top_limit_1_fired: bool = False
        self._last_bottom_limit_1_fired: bool = False
        self._last_top_limit_2_fired: bool = False
        self._last_bottom_limit_2_fired: bool = False
        # level 信号脉冲任务（fire 1 → 200ms → 0）
        self._level_pulses: dict[str, asyncio.Task] = {}
        # 虚拟电梯内部位置（不写到 car.position——executor 自己跟踪，
        # 避免虚拟 PLC 过层先改 pos、后 fire 平层被 executor 误读为"已是新楼层"）
        self._pos: int = 1
        # 门传感器模拟
        self._last_door_open_relay: int = 0
        self._last_door_close_relay: int = 0
        self._door_done_tasks: dict[str, asyncio.Task] = {}

    def start(self) -> None:
        if self._running:
            return
        if not self.io.simulate:
            return
        self._running = True
        # 同步起点：若 car.position 是 None，给个初值 1
        if self.car.position is None:
            self.car.position = 1
        elif not isinstance(self.car.position, int):
            # 拍回整数（之前可能累积了小数）
            self.car.position = int(round(self.car.position))
        self._pos = self.car.position
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self._level_pulses.values()):
            if not t.done():
                t.cancel()
        for t in list(self._door_done_tasks.values()):
            if not t.done():
                t.cancel()

    async def _run(self) -> None:
        """主循环：每 tick 检查接触器输出，驱动虚拟位置"""
        # 记录目标方向（一旦确定，每 floor_travel_time 走一层）
        moving_dir: int = 0  # +1=up, -1=down, 0=stop
        last_step_time: float = 0.0

        while self._running:
            now = asyncio.get_event_loop().time()
            try:
                await asyncio.sleep(self.tick)
            except asyncio.CancelledError:
                break

            # 门继电器模拟：door_open_relay=1 → 500ms → door_open_done=1
            self._simulate_door_sensors()

            # 读接触器 + 电机
            up = self.io.get_output(
                self.mapper.addr_output('up_contactor', self.car_id)
            )
            down = self.io.get_output(
                self.mapper.addr_output('down_contactor', self.car_id)
            )
            motor = self.io.get_output(
                self.mapper.addr_output('motor_start', self.car_id)
            )

            # 电机停 → 不动
            if not motor or (not up and not down):
                moving_dir = 0
                self._last_top_limit_1_fired = False
                self._last_bottom_limit_1_fired = False
                self._last_top_limit_2_fired = False
                self._last_bottom_limit_2_fired = False
                continue

            # 决定本 tick 的方向
            if up:
                target_dir = 1
            else:  # down
                target_dir = -1

            if moving_dir != target_dir:
                moving_dir = target_dir
                last_step_time = now

            # 每 floor_travel_time 走一层
            if now - last_step_time >= self.floor_travel_time:
                last_step_time = now
                pos = self._pos
                new_pos = pos + moving_dir

                # 1. 先检查 2 限位（如果 new_pos 越过 2 限位，立即触发 2 限位）
                if moving_dir == 1 and new_pos >= self.top_base + 1:
                    self._pos = self.top_base + 1
                    if not self._last_top_limit_2_fired:
                        self._fire_limit('top_limit_2')
                        self._last_top_limit_2_fired = True
                    continue
                if moving_dir == -1 and new_pos <= self.bottom_base - 1:
                    self._pos = self.bottom_base - 1
                    if not self._last_bottom_limit_2_fired:
                        self._fire_limit('bottom_limit_2')
                        self._last_bottom_limit_2_fired = True
                    continue

                # 2. 检查 1 限位（base）→ 触发后停在 base，不移动
                if moving_dir == 1 and new_pos >= self.top_base:
                    if not self._last_top_limit_1_fired:
                        self._pos = self.top_base
                        self._fire_limit('top_limit_1')
                        self._last_top_limit_1_fired = True
                        # 置 0 停止前进，等 executor 反转方向后重新计时；
                        # 否则 moving_dir 保持 +1，floor_travel_time 到期后
                        # new_pos = 11 + 1 = 12 → 触发 2 限位（executor 还没反转）
                        moving_dir = 0
                    continue
                if moving_dir == -1 and new_pos <= self.bottom_base:
                    if not self._last_bottom_limit_1_fired:
                        self._pos = self.bottom_base
                        self._fire_limit('bottom_limit_1')
                        self._last_bottom_limit_1_fired = True
                        moving_dir = 0
                    continue

                # 3. 正常过层 → 只 fire 平层信号，不动 car.position
                # （executor 自己从平层信号跟踪位置，
                #  这边先 fire → executor 看到旧 pos → step → 写新 pos。
                #  若这边同时写 pos，async 时序会让 executor 读到新 pos 误判"已经在新楼层"。）
                self._pos = new_pos
                # 完美平层 = level_up & level_down 都=1
                if moving_dir == 1:
                    self._pulse_level('level_up')
                    self._pulse_level('level_down')
                else:
                    self._pulse_level('level_down')
                    self._pulse_level('level_up')

    def _pulse_level(self, signal: str) -> None:
        """触发 level 信号脉冲（1 → 200ms → 0）"""
        i_addr = self.mapper.db_to_i(
            self.mapper.addr_input(signal, self.car_id)
        )
        # 先 fire 1
        self.io.simulate_input(i_addr, 1)
        # 取消旧 pulse
        old = self._level_pulses.pop(signal, None)
        if old and not old.done():
            old.cancel()
        # 200ms 后拉回 0（足够 executor 处理）
        async def reset_later():
            try:
                await asyncio.sleep(0.2)
                self.io.simulate_input(i_addr, 0)
            except asyncio.CancelledError:
                pass
        self._level_pulses[signal] = asyncio.create_task(reset_later())

    def _fire_limit(self, signal: str) -> None:
        """触发限位信号（持续 1，200ms 后自动复位）"""
        i_addr = self.mapper.db_to_i(
            self.mapper.addr_input(signal, self.car_id)
        )
        self.io.simulate_input(i_addr, 1)
        # 200ms 后自动复位
        async def reset_later():
            try:
                await asyncio.sleep(0.2)
                self.io.simulate_input(i_addr, 0)
            except asyncio.CancelledError:
                pass
        asyncio.create_task(reset_later()).add_done_callback(
            lambda t: None if t.cancelled() or not t.exception()
            else print(f'[vplc] reset_later({signal}) 异常: {t.exception()!r}'))

    def _simulate_door_sensors(self) -> None:
        """检测门继电器变化 → 模拟 door_open_done / door_close_done + 门锁状态

        物理时序:门动起来 → 锁先变（开一点就 false/true）→ 门到位 → PLC 反馈 done

        上升沿检测：
            door_open_relay 0→1  →  300ms 后 car_door_lock + floor_door_lock_{pos} → false
                                    + 500ms 后 fire door_open_done=1
            door_close_relay 0→1 →  300ms 后 car_door_lock + floor_door_lock_{pos} → true
                                    + 500ms 后 fire door_close_done=1
        继电器归 0 →  300ms 后复位 done 信号（门锁状态不归 0,等下一次继电器动作）
        """
        try:
            open_relay = self.io.get_output(
                self.mapper.addr_output('door_open_relay', self.car_id)
            )
            close_relay = self.io.get_output(
                self.mapper.addr_output('door_close_relay', self.car_id)
            )
        except KeyError:
            return

        if open_relay != self._last_door_open_relay:
            self._last_door_open_relay = open_relay
            if open_relay == 1:
                self._schedule_door_locks(action='open', delay=0.3)
                self._schedule_door_done('door_open_done', 0.5)
            else:
                self._schedule_door_done('door_open_done', 0.3)

        if close_relay != self._last_door_close_relay:
            self._last_door_close_relay = close_relay
            if close_relay == 1:
                self._schedule_door_locks(action='close', delay=0.3)
                self._schedule_door_done('door_close_done', 0.5)
            else:
                self._schedule_door_done('door_close_done', 0.3)

    def _schedule_door_locks(self, action: str, delay: float) -> None:
        """延时后 fire car_door_lock + floor_door_lock_{pos} 状态变化

        action='open':  模拟开门到位:  锁 → false
        action='close': 模拟关门到位:  锁 → true

        位置用 car.position（executor 维护）。
        """
        target_bit = 0 if action == 'open' else 1

        async def trigger():
            try:
                await asyncio.sleep(delay)
                pos = self.car.position
                if pos is None or not isinstance(pos, int):
                    return
                # car_door_lock 一定存在
                car_lock_addr = self.mapper.db_to_i(
                    self.mapper.addr_input('car_door_lock', self.car_id)
                )
                # floor_door_lock_{pos} 可能不存在（pos 越界）
                try:
                    floor_lock_addr = self.mapper.db_to_i(
                        self.mapper.addr_input(f'floor_door_lock_{pos}', self.car_id)
                    )
                except KeyError:
                    floor_lock_addr = None
                self.io.simulate_input(car_lock_addr, target_bit)
                if floor_lock_addr is not None:
                    self.io.simulate_input(floor_lock_addr, target_bit)
            except asyncio.CancelledError:
                pass
        asyncio.create_task(trigger()).add_done_callback(
            lambda t: None if t.cancelled() or not t.exception()
            else print(f'[vplc] trigger({_schedule_door_locks}) 异常: {t.exception()!r}'))

    def _schedule_door_done(self, signal: str, delay: float) -> None:
        """延时后 fire door_*_done = 1/0

        signal: 'door_open_done' 或 'door_close_done'
        delay:  延时秒数
        """
        i_addr = self.mapper.db_to_i(
            self.mapper.addr_input(signal, self.car_id)
        )
        # 取消旧任务
        old = self._door_done_tasks.pop(signal, None)
        if old and not old.done():
            old.cancel()

        async def trigger():
            try:
                await asyncio.sleep(delay)
                # 读当前继电器状态决定 done 值
                if signal == 'door_open_done':
                    relay = self.io.get_output(
                        self.mapper.addr_output('door_open_relay', self.car_id)
                    )
                else:
                    relay = self.io.get_output(
                        self.mapper.addr_output('door_close_relay', self.car_id)
                    )
                self.io.simulate_input(i_addr, relay)
            except asyncio.CancelledError:
                pass

        self._door_done_tasks[signal] = asyncio.create_task(trigger())
