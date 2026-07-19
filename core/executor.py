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
import os
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
        on_breach: Callable[[], Awaitable[None]] | None = None,
        on_light_curtain: Callable[[], Awaitable[None]] | None = None,
        on_lc_close: Callable[[], Awaitable[None]] | None = None,
        on_open_done: Callable[[], Awaitable[None]] | None = None,
        on_close_door_starting: Callable[[int], Awaitable[bool]] | None = None,
        io_write: IOClient | None = None,
        station_seek_enabled: bool = False,
        action_queue: ActionQueue | None = None,
        closing_timeout_seconds: float = 10,
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
        self.on_close_door_starting = on_close_door_starting
        self.closing_timeout_seconds = closing_timeout_seconds
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
        self.door = DoorController(io, mapper, car_id, io_write=io_write,
                                   on_breach=on_breach,
                                   on_light_curtain=on_light_curtain)

        # 日志回调（外部可注入，让 REPL 能正确显示后台任务的 print）
        # 默认走 stderr（不会被 prompt_toolkit 吞掉）
        self._log_stream = sys.stderr
        self._log_term = sys.stderr

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
        self._init_perfect_leveling_active: bool = False
        # MOVE 期间完美平层瞬态（↑1↓1 同时触发才算到一层）
        self._move_perfect_leveling_active: bool = False
        # INITIALIZE 完成后轿厢所在的基站楼层（由方向决定：up→top, down→bottom）
        self._init_base_floor: int = 1
        # INITIALIZE 到达基站后还要移动到的目标楼层（/car N init <dir> <floor>）
        self._init_target_floor: int = 1
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
        # MOVE 期间到达过的楼层（计数器崩溃检测:同一层被到达两次 → 崩溃）
        self._reached_positions: set[int] = set()
        self._last_move_direction: Direction = Direction.IDLE
        self._safety_limb_active: bool = False
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
        # 站点吸附修正完成后需要自动开门(OPEN_DOOR 被延迟)
        self._level_seek_pending_door_open: bool = False
        # Auto-seek 状态:active 时车在下跑找 (↑1↓1),找到了就停 + 激活 hold,
        # 撞 bottom_limit_1 就 fallback 入队 INITIALIZE down 1
        self._auto_seek_active: bool = False
        # flag set by _emergency_stop to prevent stale _start_action completion after door.cancel()
        self._emergency_stop_flag: bool = False

        # 预解析 level 信号 IO 地址（避免 on_io_event 热路径多次查表）
        try:
            self._level_up_i = self.mapper.addr_input('level_up', self.car_id)
            self._level_down_i = self.mapper.addr_input('level_down', self.car_id)
        except KeyError:
            self._level_up_i = None
            self._level_down_i = None

        # 检修信号边沿检测（上升沿→FAULT停车，下降沿+usermode→重新初始化）
        # None = 未 seed，第一次 service_mode 事件时从 IO cache 读真实值
        # 防止 PLC 启动时 service_mode=1 被误判为 0→1 上升沿触发 _emergency_stop
        self._last_service_mode: int | None = None

        # ===== 重量轮询器（脑干层） =====
        # 背景任务持续读 word → ADC 换算 → 更新 car.weight_kg / weight_state
        # 上层（小脑/大脑）只读缓存值，不再直接访问 IO
        self._weight_poll_task: asyncio.Task | None = None
        self._weight_poll_interval_ms: int = 500  # 正常轮询间隔（app 注入）
        # 预解析 weight word 地址
        try:
            self._weight_db_num, self._weight_byte = self.mapper.addr_word_input(
                'weight', self.car_id)
            self._weight_enabled: bool = True
        except KeyError:
            self._weight_db_num = 0
            self._weight_byte = 0
            self._weight_enabled = False
        # overweight 回调：状态变为 2 时通知上层（开门+亮灯）
        self.on_weight_overweight: Callable[[int], Awaitable[None]] | None = None
        # normalized 回调：状态从 2 降回 1/0 时通知上层（熄灯+关门）
        self.on_weight_normalized: Callable[[int], Awaitable[None]] | None = None
        # 重量变化事件回调（debug 监视器用）
        self.on_weight_event: Callable[[int, int, int, int], Awaitable[None]] | None = None

    # ===== 主循环 =====

    async def run_loop(self, queue: ActionQueue) -> None:
        """阻塞循环：取 Action → 执行 → 等传感器 → 完成 → 下一个"""
        while True:
            action = await queue.get()
            self.current_action = action
            self.waiting_sensor = None
            # H1 钩子: CLOSE_DOOR 前检查重量（weight_manager 可能拦截关门）
            if action.kind == ActionKind.CLOSE_DOOR and self.on_close_door_starting is not None:
                skip = await self.on_close_door_starting(self.car_id)
                if skip:
                    await self._complete_action()  # 跳过关门,done 通知乘客层
                    continue
            await self._start_action(action)
            # 如果是立即完成的动作（SET_DISPLAY / NOOP），已经 _complete_action 过了
            # 否则等待 on_io_event 推进

    def _log(self, msg: str) -> None:
        """始终写日志文件；终端输出受 exec_log_enabled 控制（/debug show exec_trace）"""
        if hasattr(self._log_stream, 'write'):
            self._log_stream.write(msg + '\n')
            self._log_stream.flush()
        if self.exec_log_enabled and hasattr(self, '_log_term') and hasattr(self._log_term, 'write'):
            self._log_term.write(msg + '\n')
            self._log_term.flush()

    # ===== IO 事件入口 =====

    async def on_io_event(self, event: IOEvent) -> None:
        """IOClient 收到变化时调用"""
        # 0. 检修信号在 paused 之前处理（安全信号不能被手动模式屏蔽）
        sig0 = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig0 is not None and sig0[0] == self.car_id and sig0[1] == 'service_mode':
            # Lazy seed: 首次 service_mode 事件时从 IO cache 读真实初始值
            # 防止 PLC 上电 service_mode=1 被当成上升沿误触发 _emergency_stop
            if self._last_service_mode is None:
                try:
                    svc_addr = self.mapper.addr_input('service_mode', self.car_id)
                    self._last_service_mode = self.io.get_input(svc_addr)
                except KeyError:
                    self._last_service_mode = 0
            # 同步 cache 让后续判断（如 _complete_action 走 perfect leveling）能看到 service_mode 位
            self.io.observe_input(event.i_addr, event.bit)
            prev = self._last_service_mode
            curr = event.bit
            self._last_service_mode = curr
            if curr == 1 and prev == 0:
                self._log(f'[exec] car{self.car_id} 检修信号 → FAULT + 紧急停车')
                try:
                    f_addr = self.mapper.addr_output('fault_indicator', self.car_id)
                    await self.io.set(f_addr, 1)
                except KeyError:
                    pass
                await self._emergency_stop(reason='service_mode')
                if self.action_queue is not None:
                    while not self.action_queue.empty():
                        try:
                            self.action_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                return
            elif curr == 0 and prev == 1:
                self._log(f'[exec] car{self.car_id} 检修信号释放')
                usermode = (hasattr(self, '_app') and self._app is not None
                            and getattr(self._app, '_usermode', False))
                if usermode:
                    self._log(f'[exec] car{self.car_id} usermode → 重新初始化')
                    if hasattr(self, '_app') and self._app is not None:
                        self.init_direction, target_floor = (
                            self._app._get_car_init_config(self.car_id))
                        if self.action_queue is not None:
                            await self.action_queue.put(
                                Action(ActionKind.INITIALIZE, floor=target_floor))
                return

        # 0.5 暂停模式（手动 debug 模式）：除 service_mode 外忽略所有事件
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
                        if self._level_up_i is not None and self._level_down_i is not None:
                            up = self.io.get_input(self._level_up_i)
                            dn = self.io.get_input(self._level_down_i)
                            if up == 1 and dn == 1:
                                self._auto_seek_active = False
                                self._log(f'[exec] car{self.car_id} auto-seek 找到 (↑1↓1) → 停车')
                                await self._arrive_and_brake()
                                return
                    elif sig2[1] in ('bottom_limit_1', 'top_limit_1') and event.bit == 1:
                        # 撞 1 限位 → fallback 入队 INITIALIZE down 1
                        self._auto_seek_active = False
                        self._log(f'[exec] car{self.car_id} auto-seek 撞 1 限位 → 入队 INITIALIZE down 1')
                        if self.action_queue is not None:
                            await self.action_queue.put(Action(ActionKind.INITIALIZE, floor=1))
                        return

            # 站点吸附（事件驱动）：车空闲时每次 IO 事件都检查平层信号
            if self._level_seek_active:
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
                addr = self.mapper.addr_input(limit_sig, self.car_id)
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
            await self.motor.set_speed(high_speed=False)  # 立即应用 slow_brake
            await self.motor.set_direction_indicator(reverse_dir)
            self.car.position = self._init_base_floor  # 基站位（11 或 -1）
            self._init_reverse_mode = True
            self._init_base_segment_done = False
            # 同步当前 cache 的 level 状态——DOWN 阶段的 level 脉冲(200ms)
            # 可能还在 cache 中残留(1,1),导致反向计数误以为已在第一层,
            # 过早从 0 计到 1=target 停在 base 层。标记 active=True 等下降沿
            # 后再计下个上升沿,确保从 L1 才开始计数。
            _up = self.mapper.addr_input("level_up", self.car_id)
            _dn = self.mapper.addr_input("level_down", self.car_id)
            self._init_perfect_leveling_active = (self.io.get_input(_up) == 1 and self.io.get_input(_dn) == 1)
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
                addr_up = self.mapper.addr_input('level_up', self.car_id)
                addr_down = self.mapper.addr_input('level_down', self.car_id)
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
                    try:
                        await self.display.show_number(new_pos, self.car_id)
                        self.car.display = new_pos
                    except Exception:
                        pass
                    # 标记基站段完成（首个完美平层上升沿 = 临界点）
                    # _apply_init_decel 会根据这个标记切换基-客分段逻辑
                    self._init_base_segment_done = True
                    # 到达目标 → 完成
                    if new_pos == self._init_target_floor:
                        self._log(f'[exec] car{self.car_id} INIT 到达 L{new_pos}, 全刹→停车→保持(6)')
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

            # 3b. 正常 MOVE - 完美平层(↑1↓1)才算到一层
            # 比赛传感器布局（轿顶两个传感器，井道门上方磁铁）：
            #   上行时：先触发上平层(进入平层区底部) → 再触发下平层(到达平层中心)
            #   下行时：先触发下平层 → 再触发上平层
            # 单信号触发时车还在平层区边缘，必须等 ↑1↓1 同时触发才确认到达该楼层
            if not self._init_reverse_mode and self.current_action is not None:
                kind = self.current_action.kind
                if kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
                    up_now = self.io.get_input(self._level_up_i)
                    dn_now = self.io.get_input(self._level_down_i)
                    if up_now == 1 and dn_now == 1 and not self._move_perfect_leveling_active:
                        self._move_perfect_leveling_active = True
                        direction = Direction.UP if kind == ActionKind.MOVE_UP else Direction.DOWN
                        await self._on_level_reached(direction=direction)
                    elif up_now == 0 and dn_now == 0 and self._move_perfect_leveling_active:
                        self._move_perfect_leveling_active = False
            return

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
        # 如果有当前动作也清掉（不再等传感器）
        self.current_action = None
        self.waiting_sensor = None
        self.door.cancel()  # force-complete pending door action
        self._emergency_stop_flag = True  # prevent stale _start_action completion
        # 站点吸附同步清场:否则下一次 IO event 还会触发 hold 反冲,电机重启撞限位
        self._level_seek_active = False
        self._level_seek_skip_next = False
        self._level_correct_in_progress = False
        self._level_seek_pending_door_open = False
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
        # 注:此 sleep 违反"无 sleep/wait"哲学,但实测是 PLC 物理时序的必备
        # dead time(详见 project/brake-before-stop.md)。不允许改成 cron 或删除,
        # 除非实机复现过冲 bug 且有 PLC 反馈信号替换方案(详见 feedback/practicality-first.md)
        await asyncio.sleep(0.1)
        if self._station_seek_enabled:
            self._level_seek_active = True
            self._log(f'[seek] car{self.car_id} 吸附激活')
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
            # ★ 安全保护:MOVE 运行中 target_floor 被清空(如被其他车偷走)
            # 不能 return——否则电机继续跑、位置不更新、直到撞限位。
            # 在当前平层点停车并通知上层重新决策。
            if self.current_action is not None and self.current_action.kind in (
                    ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
                self._log(f'[exec] car{self.car_id} 安全停车: target_floor=None during MOVE')
                self.decel_state = ''
                await self._arrive_and_brake()
            return

        # 1. 更新 position（经过一个平层点）
        old_pos = self.car.position
        if direction == Direction.UP:
            self.car.position += 1
        else:
            self.car.position -= 1

        new_pos = self.car.position
        remaining = target - new_pos  # 还差几层（正数=还需上行，负数=还需下行）

        # ★ 目标方向校验：target 在反方向（车 UP 但 target 在下方 / 车 DOWN 但 target 在上方）
        # 通常是 grab-hall-call 后 target 留在了反方向（grab 只缩短 target 不延长）
        # decel 逻辑里 dist = abs(target-new_pos) 可能很大 → set_speed(high=True) → 撞限位
        # 防御：发现方向不匹配立即在当前层停车，触发 PM/algorithm 重新派车
        if target is not None:
            if (direction == Direction.UP and new_pos > target) or \
                    (direction == Direction.DOWN and new_pos < target):
                self._log(f'[exec] car{self.car_id} 目标反方向: 当前 L{new_pos}, '
                          f'target=L{target}, dir={direction.value} → 紧急停车重派')
                self.decel_state = ''
                await self._arrive_and_brake()
                return

        # ★ 实时推 WS：每经过一层刷新前端楼层显示
        try:
            from web import ws_broadcast
            await ws_broadcast('car_state', {
                str(self.car_id): self._app.car_state_dict(self.car_id),
            })
        except Exception:
            pass

        # ★ 计数器崩溃检测:同一层不能被 MOVE 到达两次
        if new_pos in self._reached_positions and not self._safety_limb_active:
            await self._handle_counter_collapse()
            return
        if not self._safety_limb_active:
            self._reached_positions.add(new_pos)

        # 实时更新 7 段显示：每经过一层就刷新(中间层也显示)
        try:
            await self.display.show_number(new_pos, self.car_id)
            self.car.display = new_pos
        except Exception:
            pass

        # 每层经过都写日志（始终写文件，终端受 exec_log_enabled 控制）
        self._log(f'[exec] car{self.car_id} 经过 L{new_pos}, 距目标 {abs(remaining)} 层')
        # 终端通过 exec_log_enabled 控制，日志文件始终写入

        if self.debug:
            print(f'[exec] level reached: pos={new_pos} target={target} remaining={remaining} decel_state={self.decel_state}')

        # 2. 到达目标层 → 完全停车（复用统一刹车 _arrive_and_brake）
        if new_pos == target:
            self._log(f'[exec] car{self.car_id} 到 L{new_pos}, 全刹→停→保持(6)')
            self.decel_state = ''
            await self._arrive_and_brake()
            return

        # 3. 减速逻辑：距目标 ≥2 层高速，剩 1 层切低速 + 临时拉满刹
        dist = abs(remaining)  # 距目标还有几层
        if dist >= 2:
            if self.decel_state != 'high_speed':
                await self.motor.set_speed(high_speed=True)
                self.decel_state = 'high_speed'
        elif dist == 1:
            if self.decel_state != 'decel':
                # ★ 距 1 层到站时临时拉满刹（slow_brake_level=6 全刹）
                # 比赛轿厢惯性大，slow_brake=4 默认值在 1 层距离时刹不住
                # 实测低速+brake_4 要 9 秒才能走完 1 层（car1 L3→L2 案例）
                # 切回高速时 set_speed(high=True) 自动释放刹车，无需手动 clear
                if self.motor.slow_brake_level < 6:
                    prev_brake_level = self.motor.slow_brake_level
                    self.motor.slow_brake_level = 6
                    await self.motor.set_speed(high_speed=False)
                    self.motor.slow_brake_level = prev_brake_level
                else:
                    await self.motor.set_speed(high_speed=False)
                self.decel_state = 'decel'

    async def _level_seek_check(self) -> None:
        """站点吸附：纯事件驱动，无轮询无阻塞

        每次 IO 事件时调用（不受 current_action 限制）。
        - 上平层=0 → 车往下漂 → 释放刹车 + 慢速向上 → 等上平层=1 事件来了刹死
        - 下平层=0 → 车往上漂 → 释放刹车 + 慢速向下 → 等下平层=1 事件来了刹死
        - 修正中信号恢复(=1) → 立即刹死
        """
        if self._level_up_i is None or self._level_down_i is None:
            return
        up = self.io.get_input(self._level_up_i)
        dn = self.io.get_input(self._level_down_i)

        if self._level_correct_in_progress:
            # 修正中：任一信号恢复到完美平层 → 刹死
            if up == 1 and dn == 1:
                self._log(f'[seek] car{self.car_id} 平层恢复(↑1↓1) → 刹死')
                await self.motor.hold_stop()
                self._level_correct_in_progress = False
                # 机械稳定窗口：150ms 内忽略后续抖动事件，防止反弹循环
                self._level_seek_skip_next = True
                asyncio.get_running_loop().call_later(
                    0.15, self._clear_seek_skip)
                # ★ 平层修正完成，如果有等待开门的请求 → 推 OPEN_DOOR
                if self._level_seek_pending_door_open:
                    self._level_seek_pending_door_open = False
                    self._log(f'[seek] car{self.car_id} 平层完成 → 推 OPEN_DOOR')
                    if self.action_queue is not None:
                        await self.action_queue.put(Action(ActionKind.OPEN_DOOR))
            return

        # 未修正中：检测漂移
        if up == 1 and dn == 1:
            return  # 完美平层
        if up == 0 and dn == 0:
            return  # 两层之间，不修正
        # 缺哪个信号就往哪个方向反冲
        correct_dir = 'up' if up == 0 else 'down'
        self._level_correct_in_progress = True
        self._log(f'[seek] car{self.car_id} {correct_dir}反冲(↑{up}↓{dn})')
        await self.motor.release_brakes()
        await self.motor.start(high_speed=False, direction=correct_dir)

    def _clear_seek_skip(self) -> None:
        """150ms 机械稳定窗口结束后清除跳过标志"""
        self._level_seek_skip_next = False

    def set_station_seek(self, enabled: bool) -> None:
        """切换站点吸附总开关

        开启只改 flag,实际激活由 _arrive_and_brake 在车到站时点开。
        关闭则立即卸载:清 active + 取消任何反冲中的电机。
        """
        self._station_seek_enabled = enabled
        if not enabled:
            self._level_seek_active = False
            if self._level_correct_in_progress:
                # 反冲电机还在跑，立即刹死（不 await，fire-and-forget）
                self._level_correct_in_progress = False
                asyncio.ensure_future(self.motor.hold_stop())
            self._log(f'[seek] car{self.car_id} 吸附关闭')

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
        # 有新动作 → 退出保持模式,停止任何反冲电机
        # NOOP 不退出保持模式（算法在空闲时持续发 NOOP,每隔退出 hold 会让吸附永久不激活）
        if action.kind != ActionKind.NOOP:
            self._level_seek_active = False
            self._level_seek_pending_door_open = False
            if self._level_correct_in_progress:
                await self.motor.hold_stop()
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
                self._init_base_segment_done = False
                await self._execute_initialize()

            case ActionKind.MOVE_UP:
                await self._start_move_up()

            case ActionKind.MOVE_DOWN:
                await self._start_move_down()

            case ActionKind.OPEN_DOOR:
                # ★ 冗余开门防护：如果 PLC 已报 door_open_done=1（门已物理开到位），
                # 跳过物理开门，直接标记 OPEN。
                # 原因：MOVE 完成时外召回调 (_handle_algorithm_state_change) 与
                # 乘客队列回调 (_on_move_done) 可能同时入队两个 OPEN_DOOR，
                # 第一次开门完成后继电器仍为 1，第二次 door.open() 不会产生上升沿，
                # VPLC 的 door_open_done 延时任务不会触发，wait_done() 将永久阻塞。
                try:
                    open_done_addr = self.mapper.addr_input('door_open_done', self.car_id)
                    already_opened = self.io.get_input(open_done_addr) == 1
                except KeyError:
                    already_opened = False
                if already_opened:
                    self._log(f'[exec] car{self.car_id} OPEN_DOOR skip: door_open_done=1, complete with callback')
                    self.car.door_state = DoorState.OPEN
                    # ★ 必须调 _complete_action：触发 PM._on_door_opened 清 pickup + 熄灯
                    # PM 是幂等的（已清的 pickup 不会重复清），多次调用安全
                    await self._complete_action()
                    return
                # 平层安全校验：到站后先等稳定，再检查平层信号决定是否开门
                # 1. sleep door_open_settle_ms（让车物理停稳）
                # 2. 检查 ↑1↓1：仍在 → 停稳，开门；失效 → 站点吸附修正后再开
                if self._station_seek_enabled:
                    settle_ms = 1.5
                    if hasattr(self, '_app') and self._app is not None:
                        settle_ms = self._app.config.get(
                            'elevator', {}).get('door_open_settle_ms', 1500) / 1000.0
                    if not os.environ.get('PYTEST_CURRENT_TEST'):
                        await asyncio.sleep(settle_ms)
                    try:
                        up_addr = self.mapper.addr_input('level_up', self.car_id)
                        dn_addr = self.mapper.addr_input('level_down', self.car_id)
                    except KeyError:
                        up_addr = dn_addr = None
                    if up_addr is not None:
                        up_now = self.io.get_input(up_addr)
                        dn_now = self.io.get_input(dn_addr)
                        if not (up_now == 1 and dn_now == 1):
                            # 平层信号失效 → 站点吸附修正，延迟开门
                            self._level_seek_active = True
                            self._level_seek_pending_door_open = True
                            self._log(f'[seek] car{self.car_id} 平层失效(↑{up_now}↓{dn_now})'
                                      f' → 吸附修正后开门')
                            self.current_action = None
                            self.waiting_sensor = None
                            return
                self.car.door_state = DoorState.OPENING
                self.door.set_car_position(self.car.position)
                await self.door.open()
                result = await self.door.wait_done()
                if self._emergency_stop_flag:
                    return  # emergency stop cancelled the door action
                if result == 'wrong_floor':
                    self._log(f'[exec] car{self.car_id} OPEN wrong floor, '
                              f'emerge cleanup → 亮故障灯 → re-init')
                    # 走 _emergency_stop 标准链（清 pending/target/manual/pm pickup/level_seek）
                    await self._emergency_stop(reason='wrong_floor')
                    # 亮故障灯（_emergency_stop 不动 fault_indicator）
                    try:
                        f_addr = self.mapper.addr_output('fault_indicator', self.car_id)
                        await self.io.set(f_addr, 1)
                    except KeyError:
                        pass
                    # 主动关门（清 stale 门动作 + 同步物理状态）
                    try:
                        await self.door.close()
                        await self.door.wait_done()
                    except Exception:
                        pass
                    # 设门已关（让 algorithm dispatch 不被门状态挡）
                    self.car.door_state = DoorState.CLOSED
                    # 清 action_queue（避免 stale MOVE_UP 在 FAULT 后重启电机）
                    if self.action_queue is not None:
                        while not self.action_queue.empty():
                            try:
                                self.action_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                    # 重新初始化（读 per-car 配置）
                    if hasattr(self, '_app') and self._app is not None:
                        self.init_direction, target_floor = self._app._get_car_init_config(self.car_id)
                    else:
                        target_floor = 1
                    if self.action_queue is not None:
                        await self.action_queue.put(
                            Action(ActionKind.INITIALIZE, floor=target_floor))
                else:
                    self.car.door_state = DoorState.OPEN
                    await self._complete_action()

            case ActionKind.CLOSE_DOOR:
                # ★ 冗余关门防护：如果 PLC 已报 door_close_done=1（门已物理关好），
                # 跳过物理关门，直接标记 CLOSED。
                # 原因：cron / hall_call 松手等多路径可能向队列推入冗余 CLOSE_DOOR，
                # 此时 PLC 的 door_close_done 已是 1，再发 door_close_relay=1 不会
                # 产生上升沿事件，wait_done() 将永久阻塞导致车卡死在 CLOSING 状态。
                try:
                    close_done_addr = self.mapper.addr_input('door_close_done', self.car_id)
                    already_closed = self.io.get_input(close_done_addr) == 1
                except KeyError:
                    already_closed = False
                if already_closed:
                    self._log(f'[exec] car{self.car_id} CLOSE_DOOR skip: door_close_done=1, silent discard (no callback)')
                    self.car.door_state = DoorState.CLOSED
                    # ★ 不调 _complete_action：冗余关门不应触发 on_action_done 回调链
                    # 否则 PM._on_door_closed 会误清刚设好的 pickup_active，
                    # 导致外呼 LED 永远不熄、pending hall call 看似已派但无人响应。
                    self.current_action = None
                    self.waiting_sensor = None
                else:
                    self.car.door_state = DoorState.CLOSING
                    self._log(f'[exec] car{self.car_id} CLOSE_DOOR start: door_close_done=0, closing')
                    await self.door.close()
                    # ★ CLOSING 超时保护：如果超过配置时间未收到 door_close_done，主动查 IO
                    # 场景：PLC 边沿检测漏掉导致 door_close_done 信号永久不触发
                    # 保护策略：超时后读 IO 状态，若仍为 0 则亮故障灯并停止调度
                    if self.closing_timeout_seconds > 0:
                        try:
                            await asyncio.wait_for(
                                self.door.wait_done(), timeout=self.closing_timeout_seconds)
                        except asyncio.TimeoutError:
                            # 超时：主动查 IO 状态
                            try:
                                close_done_addr = self.mapper.addr_input(
                                    'door_close_done', self.car_id)
                                current_state = self.io.get_input(close_done_addr)
                            except KeyError:
                                current_state = 0
                            if current_state == 1:
                                # IO 已确认关好，正常完成
                                self._log(f'[exec] car{self.car_id} CLOSE_DOOR timeout but IO confirmed closed')
                                self.car.door_state = DoorState.CLOSED
                                self.door.cancel()  # 清理 wait_done 的内部 event
                                await self._complete_action()
                                return
                            else:
                                # IO 仍为 0：亮故障灯，保持 CLOSING 状态，停止调度
                                self._log(f'[exec] car{self.car_id} CLOSE_DOOR timeout: door_close_done still 0, fault!')
                                await self.io.set(
                                    self.mapper.addr_output('fault_indicator', self.car_id), 1)
                                self.car.fault = dataclasses.replace(
                                    self.car.fault, door=True)
                                return  # 不完成动作，等人工干预
                    result = await self.door.wait_done()
                    self._log(f'[exec] car{self.car_id} CLOSE_DOOR done: result={result}')
                    if self._emergency_stop_flag:
                        return
                    if result == 'cancelled':
                        pass
                    elif result == 'breach':
                        self.car.door_state = DoorState.OPEN
                        self.current_action = Action(ActionKind.OPEN_DOOR)
                    else:
                        self.car.door_state = DoorState.CLOSED
                    await self._complete_action()

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

            case ActionKind.LIGHT_OFF:
                # 注:LIGHT_OFF / LIGHT_ON 当前不被 app 控制层 dispatch,
                # 保留 handler 是为了未来 passenger_flow 模块
                # (由 IO 事件驱动,经 action_queue 推入)。
                # 控制层已剥离 _schedule_lights_off 副作用。
                await self.io.set(
                    self.mapper.addr_output('light_indicator', self.car_id), 0)
                await self._complete_action()

            case ActionKind.LIGHT_ON:
                # 同 LIGHT_OFF,留给未来 passenger_flow 模块。
                await self.io.set(
                    self.mapper.addr_output('light_indicator', self.car_id), 1)
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
            top_addr = self.mapper.addr_input('top_limit_1', self.car_id)
            at_limit = self.io.get_input(top_addr) == 1
            if not at_limit:
                await self.motor.release_brakes()
                await self.motor.set_direction_indicator('up')
                # 比赛实测：高速碰底限位会扣分，init 全程低速
                await self.motor.start(high_speed=False, direction='up')
                self.waiting_sensor = ('top_limit_1', 1)
                self.car.direction = Direction.UP
                self._log(f'[exec] car{self.car_id} 初始化: 朝 ↑ 低速运行，等待触到 1 限位（base=L{self._init_base_floor}，target=L{self._init_target_floor}）')
                return
            # 已在限位 → 直接进入反向计数模式
            self.car.position = self._init_base_floor
            self._init_reverse_mode = True
            self._init_base_segment_done = False
            # 同步当前 cache 的 level 状态——DOWN 阶段的 level 脉冲(200ms)
            # 可能还在 cache 中残留(1,1),导致反向计数误以为已在第一层,
            # 过早从 0 计到 1=target 停在 base 层。标记 active=True 等下降沿
            # 后再计下个上升沿,确保从 L1 才开始计数。
            _up = self.mapper.addr_input("level_up", self.car_id)
            _dn = self.mapper.addr_input("level_down", self.car_id)
            self._init_perfect_leveling_active = (self.io.get_input(_up) == 1 and self.io.get_input(_dn) == 1)
            self.waiting_sensor = None
            self.car.direction = Direction.UP
            await self.motor.set_direction_indicator('down')
            # 修复：上电时已在限位的情况下之前没启动电机，直接反向往下推不会动
            # 用低速启动基站段，与运行时触限位反冲行为一致
            await self.motor.release_brakes()
            await self.motor.start(high_speed=False, direction='down')
            await self.motor.set_speed(high_speed=False)  # 立即应用 slow_brake
            if await self._try_complete_init_if_at_target():
                return
            self._log(f'[exec] 初始化: 已在顶站，直接反向计数 base=L{self._init_base_floor} → target=L{self._init_target_floor}')
        else:  # down
            bot_addr = self.mapper.addr_input('bottom_limit_1', self.car_id)
            at_limit = self.io.get_input(bot_addr) == 1
            if not at_limit:
                await self.motor.release_brakes()
                await self.motor.set_direction_indicator('down')
                await self.motor.start(high_speed=False, direction='down')
                self.waiting_sensor = ('bottom_limit_1', 1)
                self.car.direction = Direction.DOWN
                self._log(f'[exec] car{self.car_id} 初始化: 朝 ↓ 低速运行，等待触到 1 限位（base=L{self._init_base_floor}，target=L{self._init_target_floor}）')
                return
            self.car.position = self._init_base_floor
            self._init_reverse_mode = True
            self._init_base_segment_done = False
            # 同步当前 cache 的 level 状态——DOWN 阶段的 level 脉冲(200ms)
            # 可能还在 cache 中残留(1,1),导致反向计数误以为已在第一层,
            # 过早从 0 计到 1=target 停在 base 层。标记 active=True 等下降沿
            # 后再计下个上升沿,确保从 L1 才开始计数。
            _up = self.mapper.addr_input("level_up", self.car_id)
            _dn = self.mapper.addr_input("level_down", self.car_id)
            self._init_perfect_leveling_active = (self.io.get_input(_up) == 1 and self.io.get_input(_dn) == 1)
            self.waiting_sensor = None
            self.car.direction = Direction.DOWN
            await self.motor.set_direction_indicator('up')
            # 修复：上电时已在底限位的情况下启动电机+低速反向往上
            await self.motor.release_brakes()
            await self.motor.start(high_speed=False, direction='up')
            await self.motor.set_speed(high_speed=False)  # 立即应用 slow_brake
            if await self._try_complete_init_if_at_target():
                return
            self._log(f'[exec] 初始化: 已在底站，直接反向计数 base=L{self._init_base_floor} → target=L{self._init_target_floor}')

    async def _start_move_up(self) -> None:
        """上行启动：释放刹车 + 点亮上行灯 + 电机（之后靠 _on_level_reached 减速）"""
        if self.car.door_state != DoorState.CLOSED:
            # 门未关好，拒绝启动电机（防止门开着时行车导致计数器崩溃）
            # 必须 _complete_action 清理 current_action，否则 executor 永久卡在 MOVE_UP
            self._log(f'[exec] car{self.car_id} MOVE_UP abort: door_state={self.car.door_state.value}')
            await self._complete_action()
            return
        self.decel_state = 'high_speed'
        self._last_level_up = 0
        self._last_move_direction = Direction.UP
        self._reached_positions.clear()
        if self.car.position is not None:
            self._reached_positions.add(self.car.position)
        # 起点已处于完美平层，标记为已计数，防止刚启动就重复计当前层
        self._move_perfect_leveling_active = True
        await self.motor.release_brakes()
        await self.motor.set_direction_indicator('up')
        # 短距离（1层）直接用低速，防止高速启动后刹不住过冲
        target = self.car.target_floor
        pos = self.car.position
        use_high = not (pos is not None and target is not None
                        and abs(target - pos) <= 1)
        await self.motor.start(high_speed=use_high, direction='up')
        self.car.direction = Direction.UP
        self.waiting_sensor = None  # 不等特定传感器，靠完美平层(↑1↓1)推进

    async def _start_move_down(self) -> None:
        """下行启动：释放刹车 + 点亮下行灯 + 电机"""
        if self.car.door_state != DoorState.CLOSED:
            # 门未关好，拒绝启动电机（防止门开着时行车导致计数器崩溃）
            # 必须 _complete_action 清理 current_action，否则 executor 永久卡在 MOVE_DOWN
            self._log(f'[exec] car{self.car_id} MOVE_DOWN abort: door_state={self.car.door_state.value}')
            await self._complete_action()
            return
        self.decel_state = 'high_speed'
        self._last_level_down = 0
        self._last_move_direction = Direction.DOWN
        self._reached_positions.clear()
        if self.car.position is not None:
            self._reached_positions.add(self.car.position)
        # 起点已处于完美平层，标记为已计数
        self._move_perfect_leveling_active = True
        await self.motor.release_brakes()
        await self.motor.set_direction_indicator('down')
        # 短距离（1层）直接用低速，防止高速启动后刹不住过冲
        target = self.car.target_floor
        pos = self.car.position
        use_high = not (pos is not None and target is not None
                        and abs(target - pos) <= 1)
        await self.motor.start(high_speed=use_high, direction='down')
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
                # 熄故障灯（wrong_floor / 检修退出 后重新初始化成功）
                try:
                    f_addr = self.mapper.addr_output('fault_indicator', self.car_id)
                    await self.io.set(f_addr, 0)
                except KeyError:
                    pass
                # 自动显示初始化层
                await self.display.show_number(self.car.position, self.car_id)
                self.car.display = self.car.position
                # 完全停车
                await self._stop_motion()

            case ActionKind.MOVE_UP | ActionKind.MOVE_DOWN:
                # MOVE_UP/MOVE_DOWN 完成时，_on_level_reached 已停车并更新 position
                # 这里只需要重置 decel_state
                self.decel_state = ''

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

    # ===== 计数器崩溃检测 + 安全回归 =====

    async def _handle_counter_collapse(self) -> None:
        """楼层计数器崩溃:亮故障灯 + 慢速寻平层 → 开门停车"""
        car = self.car
        car.state = CarState.FAULT
        car.direction = Direction.IDLE

        # 终端醒目打印
        self._log(f'\n\033[1;31m[CRASH] car{self.car_id} 楼层计数器崩溃! '
                  f'pos={car.position} 重复到达\033[0m')

        # 紧急停车（不停车状态保持 current dir 用于安全回归）
        await self.motor.stop()

        # 亮故障指示灯
        try:
            f_addr = self.mapper.addr_output('fault_indicator', self.car_id)
            await self.io.set(f_addr, 1)
        except KeyError:
            pass

        # 进入安全回归
        self._safety_limb_active = True
        recovered = await self._safety_limb_recovery()
        self._safety_limb_active = False

        if recovered:
            # 恢复成功:还原状态,清除 current_action,通知上层
            car.state = CarState.READY
            self.current_action = None
            self._move_perfect_leveling_active = False
            self._log(f'[CRASH] car{self.car_id} 已恢复为 READY,可重新调度')
        else:
            # 恢复失败:保持 FAULT
            self._log(f'[CRASH] car{self.car_id} 安全回归失败,保持 FAULT 状态')

    async def _safety_limb_recovery(self) -> bool:
        """慢速寻平层 → 触限位反冲 → 完美平层 → 开门停车

        返回 True=成功恢复, False=超时/异常

        事件驱动的主循环:不轮询,利用 asyncio.Event 等待 IO 变化;
        当前循环内用 asyncio.sleep(0.02) 小步查,因为传感器变化频率
        高于 50Hz (PLC 扫描周期 ~20ms),不会错过边沿。
        """
        car = self.car
        # 保持当前方向低速前进,至少往一个方向找平层
        direction = self._last_move_direction if hasattr(self, '_last_move_direction') else car.direction
        if direction == Direction.IDLE:
            direction = Direction.DOWN  # default fallback
        self._log(f'[CRASH] car{self.car_id} 安全回归: 开启慢速 {direction.value}')

        await self.motor.release_brakes()
        await self.motor.set_direction_indicator('up' if direction == Direction.UP else 'down')
        await self.motor.start(high_speed=False, direction='up' if direction == Direction.UP else 'down')
        await self.motor.set_speed(high_speed=False)

        recovered = False
        deadline = asyncio.get_event_loop().time() + 60  # 60s 超时 → 全停
        try:
            while True:
                if asyncio.get_event_loop().time() > deadline:
                    self._log(f'[CRASH] car{self.car_id} 安全回归超时 60s,全停')
                    return False

                await asyncio.sleep(0.02)  # 50Hz 采样

                # 限位反冲
                if direction == Direction.UP:
                    try:
                        top1 = self.io.get_input(self.mapper.addr_input('top_limit_1', self.car_id))
                    except KeyError:
                        top1 = 0
                    if top1 == 1:
                        self._log(f'[CRASH] car{self.car_id} 触顶限位 → 反向下行')
                        await self.motor.stop()
                        await self.motor.set_direction_indicator('down')
                        await self.motor.start(high_speed=False, direction='down')
                        await self.motor.set_speed(high_speed=False)
                        direction = Direction.DOWN
                        continue
                else:
                    try:
                        bot1 = self.io.get_input(self.mapper.addr_input('bottom_limit_1', self.car_id))
                    except KeyError:
                        bot1 = 0
                    if bot1 == 1:
                        self._log(f'[CRASH] car{self.car_id} 触底限位 → 反向上行')
                        await self.motor.stop()
                        await self.motor.set_direction_indicator('up')
                        await self.motor.start(high_speed=False, direction='up')
                        await self.motor.set_speed(high_speed=False)
                        direction = Direction.UP
                        continue

                # 完美平层检测
                try:
                    up = self.io.get_input(self.mapper.addr_input('level_up', self.car_id))
                    dn = self.io.get_input(self.mapper.addr_input('level_down', self.car_id))
                except KeyError:
                    continue
                if up == 1 and dn == 1:
                    await self.motor.hold_stop()
                    car.position = 0   # 标记为未知（不依赖崩溃前的计数）
                    car.direction = Direction.IDLE
                    # 熄故障灯
                    try:
                        f_addr = self.mapper.addr_output('fault_indicator', self.car_id)
                        await self.io.set(f_addr, 0)
                    except KeyError:
                        pass
                    # 开门
                    await self.door.open()
                    await self.door.wait_done()
                    car.door_state = DoorState.OPEN
                    self._log(f'\033[1;32m[CRASH] car{self.car_id} 安全回归: '
                              f'平层停车 + 开门,position=0\033[0m')
                    recovered = True
                    return True
        finally:
            if not recovered:
                await self._all_outputs_off()  # 超时/异常:停电机保险
                self._log(f'[CRASH] car{self.car_id} 安全回归失败')
            else:
                self._log(f'[CRASH] car{self.car_id} 安全回归成功')

    # ===== 重量轮询器（脑干层：IO 读 + ADC 换算） =====

    def start_weight_poller(self) -> None:
        """启动重量后台轮询（幂等）"""
        if not self._weight_enabled:
            return
        if self._weight_poll_task is not None and not self._weight_poll_task.done():
            return
        self._weight_poll_task = asyncio.create_task(self._weight_poll_loop())
        self._log(f'[exec] car{self.car_id} 重量轮询器启动, '
                  f'间隔={self._weight_poll_interval_ms}ms')

    def stop_weight_poller(self) -> None:
        """停止重量后台轮询"""
        if self._weight_poll_task is not None and not self._weight_poll_task.done():
            self._weight_poll_task.cancel()
        self._weight_poll_task = None

    async def _weight_poll_loop(self) -> None:
        """后台循环：读 word → ADC 换算 → 更新 car.weight_kg/weight_state"""
        while True:
            try:
                await self._poll_weight_once()
            except Exception as e:
                self._log(f'[exec] car{self.car_id} 重量轮询异常: {e!r}')
            interval = self._weight_poll_interval_ms / 1000.0
            await asyncio.sleep(interval)

    async def poll_weight(self) -> None:
        """按需轮询一次重量（供 weight_manager / console 调用）"""
        await self._poll_weight_once()

    async def _poll_weight_once(self) -> None:
        """单次重量轮询：读 PLC word → ADC 换算 → 更新 car 状态

        这是脑干层唯一的重量 IO 读入口。上层（小脑/大脑）只读 car.weight_kg
        和 car.weight_state 缓存值。
        """
        car = self.car
        raw = await self.io.read_word(self._weight_db_num, self._weight_byte)
        if raw is None:
            return  # read 失败，保持旧值
        # ADC 模拟量换算（Siemens 0-27648 → kg）
        if car.adc_full_scale_kg > 0:
            weight_kg = round(raw * car.adc_full_scale_kg / 27648)
        else:
            weight_kg = raw

        old_state = car.weight_state
        old_weight = car.weight_kg
        car.weight_kg = weight_kg

        # 三态机计算
        MAX = car.max_weight
        THRESHOLD = car.weight_threshold_kg
        if MAX <= 0:
            car.weight_state = 0
        elif weight_kg > MAX:
            car.weight_state = 2
        elif weight_kg >= THRESHOLD:
            car.weight_state = 1
        else:
            car.weight_state = 0

        new_state = car.weight_state

        # 状态变化回调
        if new_state == 2 and old_state != 2 and self.on_weight_overweight is not None:
            await self.on_weight_overweight(self.car_id)
        elif new_state != 2 and old_state == 2 and self.on_weight_normalized is not None:
            await self.on_weight_normalized(self.car_id)

        # debug 事件回调
        if self.on_weight_event is not None:
            if weight_kg != old_weight or new_state != old_state:
                await self.on_weight_event(self.car_id, weight_kg, old_state, new_state)

        # ★ 重量变化推 WS：每次轮询后广播（前端载重条实时更新）
        if weight_kg != old_weight or new_state != old_state:
            try:
                from web import ws_broadcast
                await ws_broadcast('weight_event', {
                    'car_id': self.car_id,
                    'weight_kg': weight_kg,
                    'state': new_state,
                })
            except Exception:
                pass