"""
executor.py —— 硬件层 FSM（动作 → IO 序列 + 等传感器确认）

职责:
    - 从 ActionQueue 取 Action
    - 把 Action 展开为具体的 IO 操作（拉接触器、继电器、7 段显示）
    - 监听 IO 事件，等传感器信号确认动作完成
    - 维护 Car 的现实状态（position / door_state / direction / fault）
    - 完成后回调 on_action_done，触发 app 重新调用 algorithm.decide()

算法层完全不需要 import 这个文件。
"""

import asyncio
import dataclasses
import sys
from typing import Awaitable, Callable

from .actions import Action, ActionKind, ActionQueue
from .controllers import DoorController, MotorController
from .display import DisplayEncoder
from .io_client import IOClient, IOEvent
from .io_mapper import IOMapper
from .player import Car, CarState, Direction, DoorState


class ActionExecutor:
    """
    硬件层执行器

    用法:
        executor = ActionExecutor(car, io, mapper, display, car_id=1, init_direction='down')
        await executor.run_loop(action_queue)  # 后台任务
    """

    def __init__(
        self,
        car: Car,
        io: IOClient,
        mapper: IOMapper,
        display: DisplayEncoder,
        car_id: int,
        init_direction: str = 'down',
        top_base_floor: int = 11,
        bottom_base_floor: int = -1,
        on_action_done: Callable[[Action], Awaitable[None]] | None = None,
        on_emergency_stop: Callable[[], Awaitable[None]] | None = None,
        io_write: IOClient | None = None,
        station_seek_enabled: bool = False,
        action_queue: ActionQueue | None = None,
    ) -> None:
        self.car = car
        self.io = io
        self.mapper = mapper
        self.display = display
        self.car_id = car_id
        self.init_direction = init_direction  # 'down' or 'up'
        self.top_base_floor = top_base_floor
        self.bottom_base_floor = bottom_base_floor
        self.on_action_done = on_action_done
        self.on_emergency_stop = on_emergency_stop
        # 站点吸附总开关（默认关，运行时由 app.set_station_seek 切换）
        self._station_seek_enabled: bool = station_seek_enabled
        # ActionQueue 引用:auto-seek 撞 1 限位 fallback 入队 INITIALIZE 用
        self.action_queue = action_queue

        # 控制器(不直接摸 IO)。
        # io_write:多车场景下 App 给每部电梯独立的写 IOClient,避免 6 部车
        #   共享一个 write_buffer 导致 tick flush 时一次 POST 30+ 个地址,
        #   S7 read-modify-write 顺序 = 车号顺序,接触器建立时间错开("偏了但没偏太多")。
        # 单车 / 测试场景传 None,回退到 io。
        self.motor = MotorController(io, mapper, car_id, io_write=io_write)
        self.door = DoorController(io, mapper, car_id, io_write=io_write)

        # 日志回调（外部可注入，让 REPL 能正确显示后台任务的 print）
        # 默认走 stderr（不会被 prompt_toolkit 吞掉）
        self._log_stream = sys.stderr

        self.current_action: Action | None = None
        # 等哪个信号 → (signal_name, expected_bit)
        self.waiting_sensor: tuple[str, int] | None = None
        # 多级减速子状态: 'high_speed' / 'decel_1' / 'decel_2' / 'decel_3'
        self.decel_state: str = ''
        # 暂停标志：手动模式下设为 True，on_io_event 直接 return，不做任何处理
        # （让手动调试模式完全 raw——2 限位、状态机、IO 写都不会干扰）
        self.paused: bool = False
        # INITIALIZE 触到 1 限位后反向运行，逐层计数完美平层直达 target_floor
        self._init_reverse_mode: bool = False
        # 是否正处于"完美平层"瞬态（用于边沿检测：上升沿 step，下降沿 reset）
        # ——代替旧的 _init_last_reverse_pos 防抖，因为现在 read cache 而非 state 字段，
        #   cache 在一次脉冲中两个 signal 都=1 时单次 edge 事件可能重复触发（虚拟 PLC
        #   fire level_up + level_down 各触发一次 dispatch）。
        self._init_perfect_leveling_active: bool = False
        # 反向开始时的时间戳:用于跳过 base 层的第一个 (1,1)
        # 如果 (1,1) 在 <500ms 内触发,说明在 base 层(车还没离开/level 抖动),
        # 跳过等下一层的 (1,1) 再计数(fix car5 L0 层过早触发)
        self._init_reverse_start_time: float | None = None
        # INITIALIZE 完成后轿厢所在的基站楼层（由方向决定：up→top, down→bottom）
        self._init_base_floor: int = 1
        # INITIALIZE 到达基站后还要移动到的目标楼层（/car N init <dir> <floor>）
        self._init_target_floor: int = 1
        # 上一次 perfection level 触发的 position 值（防止重复递减）
        self._init_last_reverse_pos: int | None = None
        # 基站段完成标记：反冲后第一个完美平层上升沿 = 临界点
        # 临界点前(基站段)全程低速；临界点后(客运段)用正常减速曲线
        # 比赛不计时，所以"慢起步"换取"刹得住"比"高速过冲"更重要
        self._init_base_segment_done: bool = False
        # 手动刹车档位（0=不刹, 1-7=不同组合）
        self.manual_brake_level: int = 0
        # 当前实际写出去的刹车组合（用于幂等性检查）
        self.manual_current_brake_state: int = 0
        # 上一次 level_up / level_down 值，用于检测上升沿（经过平层点）
        self._last_level_up: int = 0
        self._last_level_down: int = 0
        # 平层信号防抖:记录 level_up/down 上次变 0 的时间戳
        # 用于过滤电机启动瞬间的瞬态抖动(避免 1→0→1 误触发 _on_level_reached)
        self._level_up_zero_time: float | None = None
        self._level_down_zero_time: float | None = None
        # 调试输出
        self.debug = False
        self.exec_log_enabled = False  # 是否打印 [exec] 执行日志（/debug show exec_trace 控制）
        # 停车保持模式:到站后持续监听平层信号,偏离就反冲刹回
        self._level_seek_active: bool = False
        # 激活跳过一次:刚 _arrive_and_brake 激活后,跳过同 IO event 的 3d 检查,
        # 避免与 on_action_done 推入的下一个 MOVE 冲突（race condition）
        self._level_seek_skip_next: bool = False
        # 保持模式反冲中(防止重入)
        self._level_correct_in_progress: bool = False
        # 微调 Events
        self._relevel_future: asyncio.Future | None = None
        # Auto-seek 状态:active 时车在下跑找 (↑1↓1),找到了就停 + 激活 hold,
        # 撞 bottom_limit_1 就 fallback 入队 INITIALIZE down 1
        self._auto_seek_active: bool = False

    # ===== 主循环 =====

    async def run_loop(self, queue: ActionQueue) -> None:
        """阻塞循环：取 Action → 执行 → 等传感器 → 完成 → 下一个"""
        while True:
            action = await queue.get()
            self.current_action = action
            self.waiting_sensor = None
            await self._start_action(action)
            # 如果是立即完成的动作（SET_DISPLAY / NOOP），已经 _complete_action 过了
            # 否则等待 on_io_event 推进

    def _log(self, msg: str) -> None:
        """后台任务的 print：走 stderr + flush，避开 prompt_toolkit"""
        if not self.exec_log_enabled:
            return
        self._log_stream.write(msg + '\n')
        self._log_stream.flush()

    # ===== IO 事件入口 =====

    async def on_io_event(self, event: IOEvent) -> None:
        """IOClient 收到变化时调用"""
        # 0. 暂停模式（手动 debug 模式）：直接忽略所有事件
        #     让出键自由控制电机，不被 2 限位 / 紧急停止干扰
        if self.paused:
            return
        # 同步 cache（让 cache 与 event 保持一致，方便后续基于 cache 的判断——比如
        # "level_up & level_down 都=1"完美平层条件）
        self.io.observe_input(event.i_addr, event.bit)

        # 1. 更新 Car 的故障标志
        await self._update_fault_flags(event)

        # 2. 推进当前动作
        if self.current_action is None:
            # 即使没有当前动作也要检查保护逻辑（如 2 限位）
            sig2 = self.mapper.lookup_signal_by_i(event.i_addr)
            if sig2 is not None and sig2[0] == self.car_id:
                if sig2[1] in ('bottom_limit_2', 'top_limit_2'):
                    if event.bit == 1:
                        await self._emergency_stop(reason='limit_2_touched')
                    elif event.bit == 0 and self.car.state == CarState.FAULT:
                        # 2 限位释放(manual 推出去了)→ 自动恢复 READY
                        self.car.state = CarState.READY
                        self._log(f'[exec] car{self.car_id} 2 限位释放 → 自动恢复 READY')

                # Auto-seek 检查:在 active 时下跑,找 (↑1↓1) 立即停;撞 1 限位 fallback
                if self._auto_seek_active:
                    if sig2[1] in ('level_up', 'level_down'):
                        try:
                            up = self.io.get_input(self.mapper.db_to_i(
                                self.mapper.addr_input('level_up', self.car_id)))
                            dn = self.io.get_input(self.mapper.db_to_i(
                                self.mapper.addr_input('level_down', self.car_id)))
                            if up == 1 and dn == 1:
                                self._auto_seek_active = False
                                self._log(f'[exec] car{self.car_id} auto-seek 找到 (↑1↓1) → 停车')
                                await self._arrive_and_brake()
                                return
                        except KeyError:
                            pass
                    elif sig2[1] in ('bottom_limit_1', 'top_limit_1') and event.bit == 1:
                        # 撞 1 限位 → fallback 入队 INITIALIZE down 1
                        self._auto_seek_active = False
                        self._log(f'[exec] car{self.car_id} auto-seek 撞 1 限位 → 入队 INITIALIZE down 1')
                        if self.action_queue is not None:
                            await self.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))
                        return

            # 站点吸附反冲中:level_up/down=1 → 只在 (↑1↓1) 时通知等待协程
            if (sig2 is not None and sig2[0] == self.car_id
                    and sig2[1] in ('level_up', 'level_down') and event.bit == 1
                    and self._relevel_future is not None):
                try:
                    up = self.io.get_input(self.mapper.db_to_i(
                        self.mapper.addr_input('level_up', self.car_id)))
                    dn = self.io.get_input(self.mapper.db_to_i(
                        self.mapper.addr_input('level_down', self.car_id)))
                    if up == 1 and dn == 1:
                        self._relevel_future.set_result(True)
                except KeyError:
                    pass
            # 事件驱动平层检测:level_up/level_down 变化 → 检查偏离启动反冲
            await self._level_seek_check()
            return

        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None:
            return
        car_id, name = sig
        if car_id != self.car_id:
            return

        # 2.5 安全保护：当前 event 是 2 限位 = 立即急停（最常见路径）
        if name in ('bottom_limit_2', 'top_limit_2') and event.bit == 1:
            await self._emergency_stop(reason='limit_2_touched')
            return

        # 补充防护：cache 里任何 2 限位为 1 = 立即急停
        # （应对 bitmap 派发先来 1 限位 event 再来 2 限位 event 时，
        #  1 限位处理之前主动查 cache，已确保撞 2 限位时必急停）
        for limit_sig in ('top_limit_2', 'bottom_limit_2'):
            try:
                addr = self.mapper.db_to_i(self.mapper.addr_input(limit_sig, self.car_id))
                if self.io.get_input(addr) == 1:
                    await self._emergency_stop(reason=f'{limit_sig}_touched')
                    return
            except KeyError:
                pass

        # 2.6 INITIALIZE 流程：触到 1 限位 → 立即反向，逐层计数完美平层到目标
        if (self.current_action.kind == ActionKind.INITIALIZE
                and name in ('bottom_limit_1', 'top_limit_1')
                and event.bit == 1):
            # 去抖：已经进入 reverse 模式后再触到 = 机械开关抖动 / PLC 同帧重发，
            # 不要再设一次输出 + 重置计数器（会让反向计数从 base 重新开始）
            if self._init_reverse_mode:
                return
            # 不停车！反向（方向接触器互换），低速启动基站段，启动逐层计数
            # 对 init up：触顶后反向往下，每层 --position 到 target
            # 对 init down：触底后反向往上，每层 ++position 到 target
            # 反冲后第一段(基站段)走 _apply_init_decel 的低速分支；
            # 出临界点后再切换到客运段减速曲线。
            was_up = self.init_direction == 'up'
            reverse_dir = 'down' if was_up else 'up'
            await self.motor.release_brakes()  # 释放前一次停车时保持的全刹
            await self.motor.start(high_speed=False, direction=reverse_dir)
            await self.motor.set_direction_indicator(reverse_dir)
            self.car.position = self._init_base_floor  # 基站位（11 或 -1）
            self._init_reverse_mode = True
            self._init_base_segment_done = False
            # 同步当前 cache 的 level 状态——DOWN 阶段的 level 脉冲(200ms)
            # 可能还在 cache 中残留(1,1),导致反向计数误以为已在第一层,
            # 过早从 0 计到 1=target 停在 base 层。标记 active=True 等下降沿
            # 后再计下个上升沿,确保从 L1 才开始计数。
            _up = self.mapper.db_to_i(self.mapper.addr_input("level_up", self.car_id))
            _dn = self.mapper.db_to_i(self.mapper.addr_input("level_down", self.car_id))
            self._init_perfect_leveling_active = (self.io.get_input(_up) == 1 and self.io.get_input(_dn) == 1)
            self._init_reverse_start_time = asyncio.get_event_loop().time()
            self._init_last_reverse_pos = None
            self.waiting_sensor = None
            # 如果 base == target，直接完成（电梯已在目标层）
            if await self._try_complete_init_if_at_target():
                return
            dir_glyph = '↑' if not was_up else '↓'
            self._log(f'[exec] car{self.car_id} 触到 1 限位 → 反向 {dir_glyph} 全速运行，'
                      f'等待平层信号从 L{self._init_base_floor} 计数到 L{self._init_target_floor}')
            return

        # 3. 检测平层信号边沿
        if name in ('level_up', 'level_down'):
            if name == 'level_up':
                self._last_level_up = event.bit
            else:
                self._last_level_down = event.bit

            # 3a. INITIALIZE 反向后完美平层逐层计数
            # 完美平层 = level_up & level_down 同时为 1
            # 必须读 cache（不是 _last_* 状态字段）——
            #   真实硬件/VPLC 一次性 fire 两个信号，cache 里两个同步都是 1，
            #   但 dispatch 异步导致 _last_* 字段更新有先后。
            #   若读字段就漏掉"两边同时 1"的瞬间。
            #
            # 边沿检测：
            #   上升沿 (0,0 → 1,1) → step + set active
            #   下降沿 (1,1 → 0,0) → reset active
            #   其他状态不变
            if self._init_reverse_mode:
                addr_up = self.mapper.db_to_i(
                    self.mapper.addr_input('level_up', self.car_id)
                )
                addr_down = self.mapper.db_to_i(
                    self.mapper.addr_input('level_down', self.car_id)
                )
                up_now = self.io.get_input(addr_up)
                down_now = self.io.get_input(addr_down)
                # 每条 level event 都打日志（真模式调试时看时序用）
                self._log(
                    f'[exec] car{self.car_id} level {name}={event.bit} '
                    f'cache(up={up_now}, down={down_now}) '
                    f'active={self._init_perfect_leveling_active} '
                    f'pos={self.car.position} target={self._init_target_floor}'
                )
                if up_now == 1 and down_now == 1 and not self._init_perfect_leveling_active:
                    # 上升沿：刚进入完美平层区 = 过了一层
                    self._init_perfect_leveling_active = True
                    if self.car.position is None:
                        return  # reset() 后 position 还没恢复，跳过这层计数
                    pos = self.car.position
                    was_up = self.init_direction == 'up'
                    step = -1 if was_up else 1
                    new_pos = pos + step
                    self._log(f'[exec] car{self.car_id} 平层 L{pos} → L{new_pos} (目标 L{self._init_target_floor})')
                    self.car.position = new_pos
                    # 实时更新 7 段显示
                    await self.display.show_number(new_pos, self.car_id)
                    self.car.display = new_pos
                    # 标记基站段完成（首个完美平层上升沿 = 临界点）
                    # _apply_init_decel 会根据这个标记切换基-客分段逻辑
                    self._init_base_segment_done = True
                    # 到达目标 → 完成
                    if new_pos == self._init_target_floor:
                        self._log(f'[exec] car{self.car_id} INIT 到达 L{new_pos}, 全刹→停车→保持(7)')
                        self._init_reverse_mode = False
                        # 清 active 防残留影响下次 init
                        self._init_perfect_leveling_active = False
                        await self._arrive_and_brake()
                        return
                    # 还在路上：根据基-客分段应用减速曲线
                    # remaining > 0: 还需上行（init down 反冲 case）
                    # remaining < 0: 还需下行（init up 反冲 case）
                    remaining = self._init_target_floor - new_pos
                    await self._apply_init_decel(remaining)
                    return
                elif up_now == 0 and down_now == 0 and self._init_perfect_leveling_active:
                    # 下降沿：已离开完美平层区 → 重置，准备下一个上升沿
                    self._init_perfect_leveling_active = False
                    return

            # 3b. 正常 MOVE_UP/MOVE_DOWN 的减速曲线
            if event.bit == 1:
                if name == 'level_up' and self.current_action.kind == ActionKind.MOVE_UP:
                    await self._on_level_reached(direction=Direction.UP)
                elif name == 'level_down' and self.current_action.kind == ActionKind.MOVE_DOWN:
                    await self._on_level_reached(direction=Direction.DOWN)
            # 3c. 停车反冲中:只在两个平层信号同时=1时通知等待协程,不在任何 level=1 就停
            if name in ('level_up', 'level_down') and event.bit == 1 and self._relevel_future is not None:
                try:
                    up = self.io.get_input(self.mapper.db_to_i(
                        self.mapper.addr_input('level_up', self.car_id)))
                    dn = self.io.get_input(self.mapper.db_to_i(
                        self.mapper.addr_input('level_down', self.car_id)))
                    if up == 1 and dn == 1:
                        self._relevel_future.set_result(True)
                except KeyError:
                    pass
            return

        # 3d. 保持模式:每个 IO 事件(不只是 level)都检查平层偏离,
        # 发现信号偏了立刻反冲刹回,不会因为没有 level 事件而"睡觉"。
        await self._level_seek_check()

        # 4. 等待特定传感器的动作（OPEN/CLOSE_DOOR 等）
        if self.waiting_sensor is None:
            return
        wait_name, expected_bit = self.waiting_sensor
        if name == wait_name and event.bit == expected_bit:
            await self._complete_action()

    async def _emergency_stop(self, reason: str = 'unknown') -> None:
        """
        紧急停止：清所有接触器+电机+制动，置 fault 状态

        用于：
            - 2 限位（坠机限位）触发
            - 用户手动 EMERGENCY_STOP
            - 任何"丢分"危险情况
        """
        # 走 stderr + ANSI 红色 + flush，避免被 prompt_toolkit 吞掉
        self._log(f'\n\033[1;31m[exec] !!! EMERGENCY STOP: {reason} !!!\033[0m')
        await self._all_outputs_off()
        self.car.state = CarState.FAULT
        self.car.direction = Direction.IDLE
        self._init_waiting_perfect_level = False
        # 如果有当前动作也清掉（不再等传感器）
        self.current_action = None
        self.waiting_sensor = None
        # 站点吸附同步清场:否则下一次 IO event 还会触发 hold 反冲,电机重启撞限位
        self._level_seek_active = False
        self._level_seek_skip_next = False
        self._level_correct_in_progress = False
        if self._relevel_future is not None and not self._relevel_future.done():
            self._relevel_future.cancel()
        self._relevel_future = None
        # Auto-seek 同步清场:否则 limit_2 撞 FAULT 后还可能继续往 (↑1↓1) 走
        self._auto_seek_active = False
        if self.on_emergency_stop is not None:
            await self.on_emergency_stop()

    async def _try_complete_init_if_at_target(self) -> bool:
        """如果反向开始前 position == target，直接完成 INITIALIZE

        上电时车已在基站（base == target）的特殊场景——根本不用动。
        此时已经在极限位置（最高或最低基站），反冲也没意义。
        """
        if self.car.position == self._init_target_floor:
            # 显示当前层（base==target，比如 init down 0 / init up 11）
            await self.display.show_number(self.car.position, self.car_id)
            self.car.display = self.car.position
            # 清反冲相关状态（防止 _start_action 重置前已经标了 True）
            self._init_reverse_mode = False
            # 复用统一刹车：全刹+方向归零+100ms+站点吸附+complete
            await self._arrive_and_brake()
            return True
        return False

    async def _arrive_and_brake(self) -> None:
        """到站统一刹车流程：全刹→方向归零→100ms 固位→激活站点吸附→完成动作

        MOVE 和 INIT 路径共用到站逻辑（消除三处重复代码）：
        1. hold_stop 单笔 HTTP POST 同时全刹+断电机（防止惯性过冲）
        2. 灭方向灯 + 100ms 等车停稳
        3. 站点吸附使能则激活 level_seek,后续 _level_seek_check 持续监测平层
        4. _complete_action 通知 app 层

        必须在 motor 接触器/刹车已经稳定后再调 _complete_action，
        否则 app 的 _on_action_done 可能立刻发下一个动作抢占刹车。
        """
        await self.motor.hold_stop()
        self.car.direction = Direction.IDLE
        await self.motor.set_direction_indicator(None)
        await asyncio.sleep(0.1)
        if self._station_seek_enabled:
            self._level_seek_active = True
            # 跳过激活后第一次 IO event 的 _level_seek_check,
            # 避免与 _complete_action → on_action_done 推入的下一个 MOVE 冲突
            self._level_seek_skip_next = True
        await self._complete_action()

    async def _apply_init_decel(self, remaining: int) -> None:
        """INIT 反向减速曲线：基站段全程低速，客运段复用正常减速逻辑

        remaining = target - new_pos（正=还需上行，负=还需下行）
        - 基站段（_init_base_segment_done=False）：全程低速
          防"反冲第一层高速冲过平层区刹不住"
        - 客运段：复用标准减速（≥2 层高速，=1 层低速）
        """
        if not self._init_base_segment_done:
            await self.motor.set_speed(high_speed=False)
            return
        dist = abs(remaining)
        if dist >= 2:
            await self.motor.set_speed(high_speed=True)
        elif dist == 1:
            await self.motor.set_speed(high_speed=False)

    async def _on_level_reached(self, direction: Direction) -> None:
        """
        处理一次平层信号（每经过一层触发一次）。
        维护 position 追踪 + 多级减速曲线。
        """
        if self.car.position is None:
            return
        target = self.car.target_floor
        if target is None:
            return

        # 1. 更新 position（经过一个平层点）
        if direction == Direction.UP:
            self.car.position += 1
        else:
            self.car.position -= 1

        new_pos = self.car.position
        remaining = target - new_pos  # 还差几层（正数=还需上行，负数=还需下行）

        # 实时更新 7 段显示：每经过一层就刷新(中间层也显示)
        await self.display.show_number(new_pos, self.car_id)
        self.car.display = new_pos

        if self.debug:
            print(f'[exec] level reached: pos={new_pos} target={target} remaining={remaining} decel_state={self.decel_state}')

        # 2. 到达目标层 → 完全停车（复用统一刹车 _arrive_and_brake）
        if new_pos == target:
            self._log(f'[exec] car{self.car_id} 到 L{new_pos}, 全刹→停→保持(7)')
            self.decel_state = ''
            await self._arrive_and_brake()
            return

        # 3. 减速逻辑：距目标 ≥2 层高速，剩 1 层切低速，到目标刹车
        dist = abs(remaining)  # 距目标还有几层
        if dist >= 2:
            if self.decel_state != 'high_speed':
                await self.motor.set_speed(high_speed=True)
                self.decel_state = 'high_speed'
        elif dist == 1:
            if self.decel_state != 'decel':
                await self.motor.set_speed(high_speed=False)
                self.decel_state = 'decel'

    async def _level_seek_check(self) -> None:
        """保持模式:检查平层信号,偏离就反冲刹回（纯事件驱动，无轮询）

        在 _level_seek_active 时,每次 IO 事件后调用。
        如果检测到偏离✓(↑1↓1),立刻释放刹车、向偏离反方向微动、
        等待信号恢复后刹停。不 sleep / 无心跳定时器。
        """
        if self._level_correct_in_progress or not self._level_seek_active:
            return
        # 刚激活 hold 时跳过第一次检查,避免与 _complete_action 推入的下一个 MOVE 冲突
        if self._level_seek_skip_next:
            self._level_seek_skip_next = False
            return

        up = self.mapper.db_to_i(self.mapper.addr_input('level_up', self.car_id))
        dn = self.mapper.db_to_i(self.mapper.addr_input('level_down', self.car_id))
        up_now = self.io.get_input(up)
        dn_now = self.io.get_input(dn)
        if up_now == 1 and dn_now == 1:
            return  # 完美平层,不动
        if up_now == 0 and dn_now == 0:
            return  # 两层之间/停车后传感器驻留结束,不误反冲

        self._level_correct_in_progress = True
        missing_dir = 'up' if up_now == 0 else 'down'  # 缺上→往上,缺下→往下
        wait_signal = 'level_up' if up_now == 0 else 'level_down'
        self._log(f'[exec] car{self.car_id} 保持: {missing_dir}反冲(✓→↑{up_now}↓{dn_now})')
        self._relevel_future = asyncio.get_running_loop().create_future()
        await self.motor.release_brakes()
        # 低速微调:高速反冲会过冲过头、低速洗 1 次就好
        await self.motor.start(high_speed=False, direction=missing_dir)
        try:
            await asyncio.wait_for(self._relevel_future, timeout=3.0)
        except asyncio.TimeoutError:
            self._log(f'[exec] car{self.car_id} 保持反冲超时(3s)')
        finally:
            await self.motor.hold_stop()
            self._relevel_future = None
            self._level_correct_in_progress = False

    def set_station_seek(self, enabled: bool) -> None:
        """切换站点吸附总开关（不影响正在运行的车）

        开启只改 flag,实际激活由 _arrive_and_brake 在车空闲时点开。
        关闭则立即卸载:清 hold_active + 取消任何反冲中的 future。

        注意:auto-seek 逻辑由 app.set_station_seek 负责,本方法只管 flag。
        """
        self._station_seek_enabled = enabled
        if not enabled:
            # 关闭:清当前活跃 + 取消反冲
            self._level_seek_active = False
            if self._relevel_future is not None and not self._relevel_future.done():
                self._relevel_future.cancel()
            self._relevel_future = None
            self._level_correct_in_progress = False

    def is_station_seek_enabled(self) -> bool:
        return self._station_seek_enabled

    async def start_auto_seek_down(self) -> None:
        """Auto-seek 启动:低俗下跑找最近一个 (↑1↓1),找到了就停 + 激活 hold

        与 INITIALIZE 的区别:
          - 不预置位置、不需要反向计数、不走基站段
          - 只要找到 (↑1↓1) 立刻停车（依靠 hold 反冲修正过冲）
          - 撞 bottom_limit_1 才 fallback 入队 INITIALIZE（要完整的基站段确认位置）
          - 撞 limit_2 → _emergency_stop（FAULT 锁死,manual 推出后自动恢复）
        """
        if self._auto_seek_active:
            return  # 已经在跑
        await self.motor.release_brakes()
        await self.motor.set_direction_indicator('down')
        await self.motor.start(high_speed=False, direction='down')  # 低速
        self.car.direction = Direction.DOWN
        self._auto_seek_active = True
        self._log(f'[exec] car{self.car_id} auto-seek 启动:低俗下跑找 (↑1↓1)')

    # ===== Action 展开 =====

    async def _start_action(self, action: Action) -> None:
        """展开 Action 为 IO 序列，设置等待传感器"""
        # 有新动作 → 退出保持模式,取消任何正在进行的反冲
        # NOOP 不退出保持模式（算法在空闲时持续发 NOOP,每隔退出 hold 会让吸附永久不激活）
        if action.kind != ActionKind.NOOP:
            self._level_seek_active = False
            if self._relevel_future is not None and not self._relevel_future.done():
                self._relevel_future.cancel()
            self._relevel_future = None
            self._level_correct_in_progress = False
        if self.debug:
            print(f'[exec] start {action}')

        match action.kind:
            case ActionKind.INITIALIZE:
                # 保存目标楼层（ /car N init <dir> <floor> ）
                self._init_target_floor = action.floor if action.floor is not None else 1
                # 基站楼层由方向 + config 决定
                self._init_base_floor = (
                    self.bottom_base_floor if self.init_direction == 'down'
                    else self.top_base_floor
                )
                # 重置残留状态（防止旧 init 的 reverse 状态污染新动作：
                # 否则 _init_reverse_mode=True + car.position=旧 base +
                # VPLC 跑上去会撞 top_limit_2 触发 emergency）
                self._init_reverse_mode = False
                self._init_perfect_leveling_active = False
                self._init_last_reverse_pos = None
                self._init_reverse_start_time = None
                self._init_base_segment_done = False
                await self._execute_initialize()

            case ActionKind.MOVE_UP:
                await self._start_move_up()

            case ActionKind.MOVE_DOWN:
                await self._start_move_down()

            case ActionKind.OPEN_DOOR:
                await self.door.open()
                self.car.door_state = DoorState.OPENING
                self.waiting_sensor = ('door_open_done', 1)

            case ActionKind.CLOSE_DOOR:
                await self.door.close()
                self.car.door_state = DoorState.CLOSING
                self.waiting_sensor = ('door_close_done', 1)

            case ActionKind.SET_DISPLAY:
                if action.glyph is not None:
                    await self.display.show_glyph(action.glyph, self.car_id)
                elif action.floor is not None:
                    await self.display.show_number(action.floor, self.car_id)
                    self.car.display = action.floor
                # SET_DISPLAY 立即完成（无传感器等待）
                await self._complete_action()

            case ActionKind.RESET_FAULT:
                # 简化版：清所有输出（除 ready 信号外）
                await self._all_outputs_off()
                self.car.state = CarState.READY
                await self._complete_action()

            case ActionKind.EMERGENCY_STOP:
                await self._all_outputs_off()
                self.car.state = CarState.FAULT
                await self._complete_action()

            case ActionKind.NOOP:
                await self._complete_action()

    async def _execute_initialize(self) -> None:
        """
        初始化子状态机：

        流程:
            1. 全速朝 init_direction 跑
            2. 触到对应 1 限位 → 立即反向，保持全速
            3. 反向运行中，每次检测到完美平层（level_up & level_down 同时=1）
               位置向 target_floor 步进 ±1
            4. position == target_floor → 完全停车 → READY

        已触到限位时（例如上电时顶限位已触发）：直接从 base 开始反向计数。

        方向传感器映射：
            up   → 上行碰 top_limit_1 → 反向往下，从 top_base_floor 开始递减
            down → 下行碰 bottom_limit_1 → 反向往上，从 bottom_base_floor 开始递增
        """
        direction = self.init_direction
        if direction == 'up':
            top_addr = self.mapper.db_to_i(self.mapper.addr_input('top_limit_1', self.car_id))
            at_limit = self.io.get_input(top_addr) == 1
            if not at_limit:
                await self.motor.release_brakes()
                await self.motor.set_direction_indicator('up')
                await self.motor.start(high_speed=True, direction='up')
                self.waiting_sensor = ('top_limit_1', 1)
                self.car.direction = Direction.UP
                self._log(f'[exec] car{self.car_id} 初始化: 朝 ↑ 全速运行，等待触到 1 限位（base=L{self._init_base_floor}，target=L{self._init_target_floor}）')
                return
            # 已在限位 → 直接进入反向计数模式
            self.car.position = self._init_base_floor
            self._init_reverse_mode = True
            self._init_base_segment_done = False
            # 同步当前 cache 的 level 状态——DOWN 阶段的 level 脉冲(200ms)
            # 可能还在 cache 中残留(1,1),导致反向计数误以为已在第一层,
            # 过早从 0 计到 1=target 停在 base 层。标记 active=True 等下降沿
            # 后再计下个上升沿,确保从 L1 才开始计数。
            _up = self.mapper.db_to_i(self.mapper.addr_input("level_up", self.car_id))
            _dn = self.mapper.db_to_i(self.mapper.addr_input("level_down", self.car_id))
            self._init_perfect_leveling_active = (self.io.get_input(_up) == 1 and self.io.get_input(_dn) == 1)
            self._init_reverse_start_time = asyncio.get_event_loop().time()
            self._init_last_reverse_pos = None
            self.waiting_sensor = None
            self.car.direction = Direction.UP
            await self.motor.set_direction_indicator('down')
            # 修复：上电时已在限位的情况下之前没启动电机，直接反向往下推不会动
            # 用低速启动基站段，与运行时触限位反冲行为一致
            await self.motor.release_brakes()
            await self.motor.start(high_speed=False, direction='down')
            if await self._try_complete_init_if_at_target():
                return
            self._log(f'[exec] 初始化: 已在顶站，直接反向计数 base=L{self._init_base_floor} → target=L{self._init_target_floor}')
        else:  # down
            bot_addr = self.mapper.db_to_i(self.mapper.addr_input('bottom_limit_1', self.car_id))
            at_limit = self.io.get_input(bot_addr) == 1
            if not at_limit:
                await self.motor.release_brakes()
                await self.motor.set_direction_indicator('down')
                await self.motor.start(high_speed=True, direction='down')
                self.waiting_sensor = ('bottom_limit_1', 1)
                self.car.direction = Direction.DOWN
                self._log(f'[exec] car{self.car_id} 初始化: 朝 ↓ 全速运行，等待触到 1 限位（base=L{self._init_base_floor}，target=L{self._init_target_floor}）')
                return
            self.car.position = self._init_base_floor
            self._init_reverse_mode = True
            self._init_base_segment_done = False
            # 同步当前 cache 的 level 状态——DOWN 阶段的 level 脉冲(200ms)
            # 可能还在 cache 中残留(1,1),导致反向计数误以为已在第一层,
            # 过早从 0 计到 1=target 停在 base 层。标记 active=True 等下降沿
            # 后再计下个上升沿,确保从 L1 才开始计数。
            _up = self.mapper.db_to_i(self.mapper.addr_input("level_up", self.car_id))
            _dn = self.mapper.db_to_i(self.mapper.addr_input("level_down", self.car_id))
            self._init_perfect_leveling_active = (self.io.get_input(_up) == 1 and self.io.get_input(_dn) == 1)
            self._init_reverse_start_time = asyncio.get_event_loop().time()
            self._init_last_reverse_pos = None
            self.waiting_sensor = None
            self.car.direction = Direction.DOWN
            await self.motor.set_direction_indicator('up')
            # 修复：上电时已在底限位的情况下启动电机+低速反向往上
            await self.motor.release_brakes()
            await self.motor.start(high_speed=False, direction='up')
            if await self._try_complete_init_if_at_target():
                return
            self._log(f'[exec] 初始化: 已在底站，直接反向计数 base=L{self._init_base_floor} → target=L{self._init_target_floor}')

    async def _start_move_up(self) -> None:
        """上行启动：释放刹车 + 点亮上行灯 + 高速 + 上 + 电机（之后靠 _on_level_reached 减速）"""
        self.decel_state = 'high_speed'
        self._last_level_up = 0
        await self.motor.release_brakes()
        await self.motor.set_direction_indicator('up')
        await self.motor.start(high_speed=True, direction='up')
        self.car.direction = Direction.UP
        self.waiting_sensor = None  # 不等特定传感器，靠 level_up 边沿推进

    async def _start_move_down(self) -> None:
        """下行启动：释放刹车 + 点亮下行灯 + 高速 + 下 + 电机"""
        self.decel_state = 'high_speed'
        self._last_level_down = 0
        await self.motor.release_brakes()
        await self.motor.set_direction_indicator('down')
        await self.motor.start(high_speed=True, direction='down')
        self.car.direction = Direction.DOWN
        self.waiting_sensor = None

    async def _stop_motion(self) -> None:
        """完全停车：清所有电机/接触器/制动"""
        await self.motor.stop()

    async def _complete_action(self) -> None:
        """动作完成：更新 Car 状态 + 清接触器 + 触发回调"""
        action = self.current_action
        self.current_action = None
        self.waiting_sensor = None

        if self.debug:
            print(f'[exec] done {action}')

        if action is None:
            return

        # 根据动作类型更新 Car 状态
        match action.kind:
            case ActionKind.INITIALIZE:
                # 反向逐层计数已在 on_io_event 中设置了正确 position
                self.car.state = CarState.READY
                # 初始化完成 → 清掉端站限位 fault 标志
                # (到达基站是"成功定位"而不是"撞限位故障")
                self.car.fault = dataclasses.replace(
                    self.car.fault, bottom_limit=False, top_limit=False
                )
                # 自动显示初始化层
                await self.display.show_number(self.car.position, self.car_id)
                self.car.display = self.car.position
                # 完全停车
                await self._stop_motion()

            case ActionKind.MOVE_UP | ActionKind.MOVE_DOWN:
                # MOVE_UP/MOVE_DOWN 完成时，_on_level_reached 已停车并更新 position
                # 这里只需要重置 decel_state
                self.decel_state = ''

            case ActionKind.OPEN_DOOR:
                self.car.door_state = DoorState.OPEN
            case ActionKind.CLOSE_DOOR:
                self.car.door_state = DoorState.CLOSED

            case _:
                pass

        if self.on_action_done is not None:
            await self.on_action_done(action)

    # ===== 内部辅助 =====

    async def _all_outputs_off(self) -> None:
        """清所有输出（除 ready/指示灯/数码管外），motor/door 由控制器管理"""
        # 电机 + 门走各自的控制器（不直接摸 mapper/io）
        await self.motor.all_off()
        await self.door.all_off()
        # 其余非控制信号（指示灯、LED 等）在此逐一清零
        for sig in self.mapper.all_output_signals(self.car_id):
            if sig in ('ready',
                       # motor 信号已由 MotorController.all_off 处理
                       'up_contactor', 'down_contactor',
                       'high_speed_contactor', 'low_speed_contactor',
                       'motor_start', 'brake_1', 'brake_2', 'brake_3',
                       # door 信号已由 DoorController.all_off 处理
                       'door_open_relay', 'door_close_relay',
                       'segment_a', 'segment_b', 'segment_c', 'segment_d',
                       'segment_e', 'segment_f', 'segment_g', 'segment_h',
                       'segment_i', 'segment_j', 'segment_k', 'segment_l',
                       'segment_m', 'cabin_button_led_1', 'cabin_button_led_2',
                       'cabin_button_led_3', 'cabin_button_led_4', 'cabin_button_led_5',
                       'cabin_button_led_6', 'cabin_button_led_7', 'cabin_button_led_8',
                       'cabin_button_led_9', 'cabin_button_led_10',
                       'car_door_lock_led', 'up_indicator', 'down_indicator',
                       'fault_indicator', 'light_indicator', 'fan_indicator',
                       'full_load_indicator'):
                continue
            try:
                await self.io.set(self.mapper.addr_output(sig, self.car_id), 0)
            except KeyError:
                pass

    async def _update_fault_flags(self, event: IOEvent) -> None:
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None:
            return
        car_id, name = sig
        if car_id != self.car_id:
            return

        updates: dict[str, bool] = {}
        match name:
            case 'overload':
                updates['overload'] = event.bit == 1
            case 'service_mode':
                updates['service_mode'] = event.bit == 1
            case 'light_curtain':
                updates['light_curtain'] = event.bit == 1
            case 'top_limit_1' | 'top_limit_2':
                updates['top_limit'] = event.bit == 1
            case 'bottom_limit_1' | 'bottom_limit_2':
                updates['bottom_limit'] = event.bit == 1
        if updates:
            self.car.fault = dataclasses.replace(self.car.fault, **updates)