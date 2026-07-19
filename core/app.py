"""
app.py —— 异步装配与主循环

职责:
    - 加载所有 config
    - 装配多轿厢（6 部）：每部拥有独立的 Car / Executor / ActionQueue / VirtualPLC
    - 共享 IOClient / IOMapper / DisplayEncoder / Algorithm
    - IO 事件按 car_id 路由到对应的 executor
    - 暴露高层 API 给 console 调用（call / reset / status / etc.）
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .actions import Action, ActionKind, ActionQueue
from .algorithm import ElevatorAlgorithm, get_algorithm
from .cron import Cron, CronJob, EventRule
from .display import DisplayEncoder
from .executor import ActionExecutor
from .io_client import IOClient, IOEvent
from .io_mapper import IOMapper
from .player import Car, CarState, Direction, DoorState, IndicatorState
from .ui import UiController
from .virtual_plc import VirtualPLC
from .weight_manager import WeightManager
from .logging import init_log

# PassengerManager is an optional plug-in module.
# If missing, the cerebellum runs normally; usermode requires it.
try:
    from .passenger import PassengerManager
except ImportError:
    PassengerManager = None  # type: ignore

# 默认轿厢范围(若 config.yaml 里 elevator.car_ids 未配置)
DEFAULT_CAR_IDS = [1, 2, 3, 4, 5, 6]


def _fire_and_forget(coro, *, name: str = '') -> asyncio.Task:
    """创建后台 task 并附加异常日志回调（防止 create_task 吞异常）"""
    task = asyncio.create_task(coro, name=name or None)

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            label = f' ({name})' if name else ''
            print(f'[app] 后台 task{label} 异常: {exc!r}')

    task.add_done_callback(_on_done)
    return task


class App:
    def __init__(
        self,
        config_path: str | Path,
        io_config_path: str | Path,
        display_config_path: str | Path,
        simulate: bool = False,
    ) -> None:
        self.config_path = Path(config_path)
        self.display_config_path = Path(display_config_path)
        self.simulate = simulate

        self.config: dict[str, Any] = {}
        self._load_config()

        # 根据 io_profile 解析 io_config 路径（冷切换，启动时确定）
        self.io_config_path = self._resolve_io_config_path(Path(io_config_path))

        # ===== 共享组件 =====
        io2http = self.config['io2http']
        self.io = IOClient(
            http_url=io2http['http_url'],
            ws_url=io2http['ws_url'],
            simulate=simulate,
            alias=io2http.get('alias', 'plc'),
            word_read_url=io2http.get('word_read_url'),
            word_read_alias=io2http.get('word_read_alias', 'weight'),
            word_read_timeout=io2http.get('weight_read_timeout_ms', 500) / 1000.0,
            debug=False,
            tick_interval_ms=io2http.get('tick_interval_ms', 100),
        )
        self.mapper = IOMapper(self.io_config_path)
        self.algorithm: ElevatorAlgorithm = get_algorithm(
            self.config['algorithm']['name']
        )

        # ===== 多轿厢 =====
        self.current_car_id: int = 1
        self.cars: dict[int, Car] = {}
        self.executors: dict[int, ActionExecutor] = {}
        self.action_queues: dict[int, ActionQueue] = {}
        self.pending_calls: dict[int, list[int]] = {}
        self.manual_mode: dict[int, bool] = {}
        self._executor_tasks: dict[int, asyncio.Task] = {}

        building = self.config['building']
        # 从 config 读 car_ids,默认 [1..6]
        # 注意:car_ids 在启动时确定,/reload 不会动态增删车
        self.car_ids: list[int] = list(
            self.config.get('elevator', {}).get('car_ids', DEFAULT_CAR_IDS)
        )

        # 关键:每部电梯独立的 io_write(写通道),避免 6 部车共享一个
        # write_buffer + 一次 tick flush 出 30+ 个地址,S7 read-modify-write
        # 顺序就是车号顺序,各车接触器实际建立时间错开("偏了但没偏太多")。
        # 共享 self.io 的 input/output cache(读)让"只写"实例也能看到最新 IO 状态。
        io2http_cfg = self.config['io2http']
        self._shared_caches = {
            'input': self.io._input_cache,
            'output': self.io._output_cache,
        }
        self.io_write: dict[int, IOClient] = {}
        for cid in self.car_ids:
            self.io_write[cid] = IOClient(
                http_url=io2http_cfg['http_url'],
                ws_url=None,  # 不连 WS,bitmap 由 self.io 负责
                alias=io2http_cfg.get('alias', 'plc'),
                simulate=simulate,
                debug=False,
                tick_interval_ms=io2http_cfg.get('tick_interval_ms', 100),
                shared_input_cache=self._shared_caches,
            )

        # display 也用 per-car io,避免 6 部车同时更新显示也拥堵
        # 但 display 写入不紧急(异步 tick 合并即可),共享 self.io 也行
        # 这里为了简单,display 仍用 self.io(只影响 tick 自动 flush,不拥堵 critical 操作)
        self.display = DisplayEncoder(display_config_path, io=self.io, mapper=self.mapper)

        for cid in self.car_ids:
            self.cars[cid] = Car(car_id=cid)
            # 注入每车重量配置
            per_car_weight = self.config.get('elevator', {}).get('per_car_weight', {})
            if str(cid) in per_car_weight:
                w_cfg = per_car_weight[str(cid)]
                self.cars[cid].max_weight = w_cfg.get('max_weight', 0)
                self.cars[cid].weight_threshold_kg = int(
                    w_cfg.get('max_weight', 0) * w_cfg.get('threshold', 0.95)
                )
                self.cars[cid].adc_full_scale_kg = w_cfg.get('adc_full_scale_kg', 0)
            self.action_queues[cid] = ActionQueue()
            self.pending_calls[cid] = []
            self.manual_mode[cid] = False
            self.executors[cid] = ActionExecutor(
                car=self.cars[cid],
                io=self.io,
                io_write=self.io_write[cid],
                mapper=self.mapper,
                display=self.display,
                car_id=cid,
                init_direction=self.config['elevator']['initialization_direction'],
                top_base_floor=building['top_base_floor'],
                bottom_base_floor=building['bottom_base_floor'],
                on_action_done=self._make_on_action_done(cid),
                on_emergency_stop=self._make_on_emergency_stop(cid),
                on_breach=self._make_on_breach(cid),
                on_light_curtain=self._make_on_light_curtain(cid),
                on_lc_close=self._make_on_lc_close(cid),
                on_open_done=self._make_on_open_done_fault_off(cid),
                station_seek_enabled=self.config['elevator'].get('station_seek', False),
                action_queue=self.action_queues[cid],
                closing_timeout_seconds=self.config.get('elevator', {}).get(
                    'door_complete_timeout', 8),
            )
            self.executors[cid]._app = self

        # 日志:替换 sys.stderr 为 TeeStderr → 所有模块 stderr 输出自动进文件+终端
        # executor._log 用纯文件 _log_file（始终写文件,终端受 exec_log_enabled 控制）
        if not os.environ.get('PYTEST_CURRENT_TEST'):
            self._log_tee, self._log_file = init_log('logs')
            self._original_stderr = sys.stderr  # 保存原始 stderr 引用
            sys.stderr = self._log_tee
            # 注入输出日志回调（翻译 DB 地址 → 信号名 + 写纯文件）
            self.io._log_set = self._log_output
            for cid in self.car_ids:
                self.io_write[cid]._log_set = self._log_output
                self.executors[cid]._log_stream = self._log_file
                self.executors[cid]._log_term = self._original_stderr
                self.executors[cid].door._log_file = self._log_file

        # 应用 slow_brake 配置（低速阶段叠加刹车档位）
        _slow_brake = self.config['elevator'].get('slow_brake', 0)
        for cid in self.car_ids:
            self.executors[cid].motor.slow_brake_level = _slow_brake

        # 装配 per-car UiController(与 executors 平级,游戏 entity-component 模式)
        # UI 是电梯实体的属性,通过 app.ui[cid].set_xxx() 写,car.ui.xxx 读
        self.ui: dict[int, UiController] = {}
        for cid in self.car_ids:
            self.ui[cid] = UiController(
                io_write=self.io_write[cid],
                mapper=self.mapper,
                car_id=cid,
                car=self.cars[cid],
            )

        # Hall indicator 是建筑级信号(car_id=0),不属于任何轿厢
        # 状态单独存在 App 上(不挂在 Car 上)
        self._hall_indicator_state: dict[tuple[int, str], bool] = {}

        # ===== 重量三态机管理器（小脑模块）=====
        # 重量 IO 读 + ADC 换算已下沉到 executor._poll_weight_once()（脑干层）。
        # weight_manager 只负责状态变化时的副作用动作（开门/亮灯/关门）。
        self.weight_manager = WeightManager(self)
        weight_poll_ms = self.config.get('elevator', {}).get('weight_poll_interval_ms', 500)
        for cid in self.car_ids:
            exe = self.executors[cid]
            # H1: 关门重量检查（读 car.weight_state 缓存）
            exe.on_close_door_starting = (
                self.weight_manager.on_close_door_starting
            )
            # 轮询间隔注入
            exe._weight_poll_interval_ms = weight_poll_ms
            # 状态变化回调：executor 轮询 → weight_manager 副作用
            exe.on_weight_overweight = self.weight_manager.on_overweight
            exe.on_weight_normalized = self.weight_manager.on_normalized
        # 外召灯 observer 列表（事件驱动，debug 监视器注册）
        self._hall_light_observers: list = []
        # 外召按钮边沿检测状态（防止 PLC 持续上报 bit=1 导致重复派车）
        self._hall_call_last_state: dict[str, int] = {}

        # 同轿厢互斥锁(/door 同车不能并发)
        # 用 bool 标志即可:asyncio 是协作式调度,await done_event.wait()
        # 期间事件循环可调度其他 coroutine,但其他 /door 调用会看到 busy=True 而退出。
        self._door_busy: dict[int, bool] = {cid: False for cid in self.car_ids}
        # 是否在终端打印 UI 事件（外呼/轿内按钮等）。默认关
        # 日志文件始终记录，不受此标志影响
        self._print_ui_events: bool = False

        self._executor_task: asyncio.Task | None = None
        self.debug = False
        self._usermode = False
        self.cron = Cron()
        self.pending_call_origin: dict[int, dict[int, str]] = {}
        for cid in self.car_ids:
            self.pending_call_origin[cid] = {}

        # ===== 上层乘客交互（大脑 — 可选插件） =====
        self.ui_config_path = self.config_path.parent / 'ui_config.yaml'
        self.pm: PassengerManager | None = None
        if PassengerManager is not None:
            self.pm = PassengerManager(self, self.ui_config_path)

    @property
    def car(self) -> Car:
        """当前选中的轿厢（console 兼容）"""
        return self.cars[self.current_car_id]

    @property
    def executor(self) -> ActionExecutor:
        """当前选中的 executor（console 兼容）"""
        return self.executors[self.current_car_id]

    @property
    def action_queue(self) -> ActionQueue:
        """当前选中的 action queue（console 兼容）"""
        return self.action_queues[self.current_car_id]

    def _load_config(self) -> None:
        with self.config_path.open('r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

    def _log_output(self, db_addr: str, bit: int) -> None:
        """输出日志回调:翻译 DB 地址 → 信号名,写纯文件（不刷终端）"""
        sig = self.mapper._output_db_to_signal.get(db_addr)
        if sig:
            cid, name = sig
            msg = f'[io:out] car{cid} {name} = {bit}\n'
        else:
            msg = f'[io:out] {db_addr} = {bit}\n'
        if hasattr(self, '_log_file') and hasattr(self._log_file, 'write'):
            self._log_file.write(msg)
            self._log_file.flush()

    def _resolve_io_config_path(self, default_path: Path) -> Path:
        """根据 config.yaml 的 io_profile 字段解析 io_config 文件路径
        
        config/io_profile/{io_profile}.yaml 存在 → 用 profile 文件
        否则 fallback 到 default_path（兼容旧 config/io_config.yaml）
        """
        profile = self.config.get('io_profile', '')
        if profile:
            profile_dir = self.config_path.parent / 'io_profile'
            profile_path = profile_dir / f'{profile}.yaml'
            if profile_path.exists():
                return profile_path
        return default_path

    # ===== 生命周期 =====

    async def start(self) -> None:
        await self.io.start()
        if hasattr(self.io, 'set_known_i_addresses'):
            self.io.set_known_i_addresses(set(self.mapper.lookup_all_i_addresses()))
        self.io.add_listener(self._on_io_event)
        # 小脑监听乘客事件 → 路由到大脑流程管理
        self.io.add_listener(self._on_hall_call_event)
        self.io.add_listener(self._on_cabin_button_event)
        self.io.add_listener(self._on_door_button_event)
        # light_curtain handled by DoorController internally
        self.cron.register(self.io, self.mapper)
        await self.cron.start()
        # 起 6 部电梯各自的写 IOClient(共享 input_cache,各自独立 flush)
        for io_w in self.io_write.values():
            await io_w.start()

        building = self.config.get('building', {})
        for cid in self.car_ids:
            task = asyncio.create_task(
                self.executors[cid].run_loop(self.action_queues[cid])
            )
            self._executor_tasks[cid] = task

        if self.simulate:
            self.virtual_plcs: dict[int, VirtualPLC] = {}
            for cid in self.car_ids:
                vplc = VirtualPLC(
                    io=self.io,
                    mapper=self.mapper,
                    car=self.cars[cid],
                    car_id=cid,
                    top_base=building.get('top_base_floor', 11),
                    bottom_base=building.get('bottom_base_floor', 0),
                    top_floor=building.get('max_floor', 10),
                    bottom_floor=building.get('min_floor', 1),
                )
                self.virtual_plcs[cid] = vplc
                vplc.start()
            print(f'[vplc] 已启动 {len(self.car_ids)} 部虚拟 PLC')

        # 启动每部电梯的重量后台轮询器（脑干层）
        # 重量改为按需轮询（关门时 / status 命令时），不再后台持续跑
        # for cid in self.car_ids:
        #     self.executors[cid].start_weight_poller()

        # ===== HMI Web 服务（测试模式下跳过，避免端口冲突）=====
        if not os.environ.get('PYTEST_CURRENT_TEST'):
            web_port = self.config.get('web', {}).get('port', 10010)
            try:
                from web import start_web_server
                self._web_runner = await start_web_server(self, web_port)
            except ImportError:
                pass
            except Exception as e:
                print(f'[web] 启动失败: {e}')

    async def stop(self) -> None:
        # 停止 Web 服务
        if hasattr(self, '_web_runner'):
            await self._web_runner.cleanup()
        # 停止站点吸附（清标志 + 停反冲电机）
        for cid in self.car_ids:
            exe = self.executors[cid]
            exe._level_seek_active = False
            if exe._level_correct_in_progress:
                exe._level_correct_in_progress = False
                await exe.motor.hold_stop()
        # 停止重量轮询器
        for cid in self.car_ids:
            self.executors[cid].stop_weight_poller()
        await self.cron.stop()
        if self.simulate:
            for vplc in getattr(self, 'virtual_plcs', {}).values():
                await vplc.stop()
        for task in self._executor_tasks.values():
            if task and not task.done():
                task.cancel()
        for io_w in self.io_write.values():
            await io_w.stop()
        await self.io.stop()
        # 关闭日志文件，恢复原始 stderr
        if hasattr(self, '_log_tee'):
            sys.stderr = self._original_stderr
            self._log_tee.close()

    # ===== IO 事件路由（按 car_id） =====

    def car_state_dict(self, car_id: int) -> dict:
        """Car 状态 → JSON-safe dict（供 HMI / WebSocket 用）"""
        car = self.cars[car_id]
        per_car_w = self.config.get('elevator', {}).get('per_car_weight', {}).get(str(car_id), {})
        return {
            'car_id': car_id,
            'state': car.state.value if car.state else 'unknown',
            'position': car.position,
            'target_floor': car.target_floor,
            'direction': car.direction.value if car.direction else 'idle',
            'door_state': car.door_state.value if car.door_state else 'closed',
            'display': car.display,
            'fault': car.fault.any_active(),
            'weight_state': getattr(car, 'weight_state', 0),
            'weight_kg': getattr(car, 'weight_kg', 0),
            'max_weight': per_car_w.get('max_weight', 0),
            'driver_mode': getattr(car, 'driver_mode', False),
            'pending_calls': list(self.pending_calls.get(car_id, [])),
        }

    def _log_ui(self, msg: str) -> None:
        """记录 UI 事件到日志文件，终端受 _print_ui_events 控制"""
        if hasattr(self, '_log_file') and hasattr(self._log_file, 'write'):
            self._log_file.write(msg + '\n')
            self._log_file.flush()
        if self._print_ui_events and hasattr(self, '_original_stderr'):
            self._original_stderr.write(msg + '\n')
            self._original_stderr.flush()

    async def _on_io_event(self, event: IOEvent) -> None:
        """IO 变化事件 → 查找归属轿厢 → 交给对应 executor"""
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig:
            cid, name = sig
            # 写纯文件日志（不刷终端，bitmap 上电瞬间太多）
            if hasattr(self, '_log_file') and hasattr(self._log_file, 'write'):
                self._log_file.write(f'[io] car{cid} {name} = {event.bit}\n')
                self._log_file.flush()
        cid = sig[0] if sig and sig[0] is not None else self.current_car_id
        if cid in self.executors:
            await self.executors[cid].on_io_event(event)

    # ===== 重量查询（给 REPL /car status 用） =====

    async def _read_car_weight(self, car_id: int) -> int | None:
        """按需轮询一次重量再返回（供 /car status 用）

        返回 None = 当前 profile 无 weight_word 配置
        """
        exe = self.executors[car_id]
        if not exe._weight_enabled:
            return None
        await exe.poll_weight()
        return self.cars[car_id].weight_kg

    def is_floor_door_open(self, car_id: int, floor: int) -> bool:
        """检查楼层门锁是否已开（floor_door_lock_{floor} == 0）"""
        try:
            i_addr = self.mapper.addr_input(f'floor_door_lock_{floor}', car_id)
            return self.io.get_input(i_addr) == 0
        except KeyError:
            return False

    def is_hall_button_held(self, floor: int, direction: str) -> bool:
        """检查外召按钮是否物理按住（hall_call_{direction}_{floor} == 1）"""
        try:
            i_addr = self.mapper.addr_input(f'hall_call_{direction}_{floor}', 0)
            return self.io.get_input(i_addr) == 1
        except KeyError:
            return False

    def is_door_open_button_held(self, car_id: int) -> bool:
        """检查开门按钮是否物理按住（door_open_button == 1）"""
        try:
            i_addr = self.mapper.addr_input('door_open_button', car_id)
            return self.io.get_input(i_addr) == 1
        except KeyError:
            return False

    def is_light_curtain_active(self, car_id: int) -> bool:
        """检查光幕是否触发（light_curtain == 1）"""
        try:
            i_addr = self.mapper.addr_input('light_curtain', car_id)
            return self.io.get_input(i_addr) == 1
        except KeyError:
            return False

    # ===== IO 事件监听器（小脑 — 纯解析+转发到大脑） =====

    async def _on_hall_call_event(self, event: IOEvent) -> None:
        if not self._usermode or self.pm is None:
            return
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None or sig[0] != 0:
            return  # hall_call is global signal (car_id=0)
        signal_name = sig[1]
        direction: str | None = None
        floor: int | None = None
        if signal_name.startswith('hall_call_up_'):
            direction = 'up'
            try: floor = int(signal_name[len('hall_call_up_'):])
            except ValueError: return
        elif signal_name.startswith('hall_call_down_'):
            direction = 'down'
            try: floor = int(signal_name[len('hall_call_down_'):])
            except ValueError: return
        else: return
        # 边沿检测：只在 0→1（按下）和 1→0（松开）时转发，中间持续不刷
        key = signal_name
        last = self._hall_call_last_state.get(key, 0)
        if last == event.bit:
            return  # 无变化，跳过
        self._hall_call_last_state[key] = event.bit
        label = '按下' if event.bit else '松开'
        self._log_ui(f'[io] 外呼 {direction}@L{floor} {label}')
        await self.pm.on_hall_call(floor, direction, event.bit)

    async def _on_cabin_button_event(self, event: IOEvent) -> None:
        if not self._usermode or self.pm is None:
            return
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None or sig[0] not in self.car_ids:
            return
        cid, signal_name = sig
        if not signal_name.startswith('cabin_button_'):
            return
        try: floor = int(signal_name[len('cabin_button_'):])
        except ValueError: return

        if event.bit == 1:
            # 内召按下 = 确定有人 → 设为 1
            self._log_ui(f'[io] car{cid} 轿内按钮 L{floor} 按下')
            self.cars[cid].human_presence = 1
            await self.cron.cancel(self.pm._human_presence_job_name(cid))
            await self.ui[cid].set_cabin_button_led(floor, True)
            try:
                from web import ws_broadcast
                await ws_broadcast('cabin_led', {'car_id': cid, 'floor': floor, 'on': True})
            except Exception:
                pass
            await self.pm.on_cabin_button(cid, floor)
        else:
            self._log_ui(f'[io] car{cid} 轿内按钮 L{floor} 松开')
            # 内召松开：若按钮楼层 == 当前楼层 → 灭灯
            # （不在当前楼层的灯由 on_door_opened 到站时统一灭）
            car = self.cars[cid]
            if car.position == floor:
                await self.ui[cid].set_cabin_button_led(floor, False)
                try:
                    from web import ws_broadcast
                    await ws_broadcast('cabin_led', {'car_id': cid, 'floor': floor, 'on': False})
                except Exception:
                    pass

    async def _on_door_button_event(self, event: IOEvent) -> None:
        if not self._usermode or self.pm is None:
            return
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None or sig[0] not in self.car_ids:
            return
        cid, signal_name = sig
        # ★ 只转发开门/关门按钮信号，忽略其他所有信号
        if signal_name not in ('door_open_button', 'door_close_button'):
            return
        label = '按下' if event.bit else '松开'
        self._log_ui(f'[io] car{cid} {signal_name} {label}')
        # H3 钩子:开门按钮按下时更新重量状态
        if signal_name == 'door_open_button' and event.bit == 1:
            await self.weight_manager.on_door_open_button_pressed(cid)
        # bit=1=按下, bit=0=松开都需转发到大脑(尤其开门松开后要启关门 cron)
        await self.pm.on_door_button(cid, signal_name, event.bit)

    # _on_light_curtain_event deleted — DoorController manages light curtain internally
    # with on_light_curtain callback → human_presence + PM notification

    # ===== 协调（按轿厢） =====

    async def _tick(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        if self.debug:
            print(f'[tick] car={cid} pos={self.cars[cid].position} '
                  f'pending={self.pending_calls[cid]}')
        actions = self.algorithm.decide(self.cars[cid], self.pending_calls[cid])
        self._log_ui(f'[tick] car{cid} pos={self.cars[cid].position} pending={self.pending_calls[cid]} door={self.cars[cid].door_state.value} actions={[a.kind.value for a in actions]}')
        for action in actions:
            # skip duplicate MOVE if executor already has one running
            if action.kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
                exe = self.executors[cid]
                if exe.current_action is not None and exe.current_action.kind in (
                        ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
                    continue  # already moving — pending will be picked up on completion
            # 推 MOVE 时如果 target_floor 还没设,从 pending[0] 取(FIFO)
            if action.kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN) and self.cars[cid].target_floor is None:
                if self.pending_calls[cid]:
                    self.cars[cid].target_floor = self.pending_calls[cid][0]
            if self.debug:
                print(f'[tick]   → {action}')
            await self.action_queues[cid].put(action)
            # 车开始移动 → 检查是否需要让其他空闲车回 L1
            if action.kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
                await self._auto_park_check(cid)

    async def _auto_park_check(self, moving_car_id: int) -> None:
        """当一部车开始移动时，检查是否需要让其他空闲车回 L1 待命

        规则：
        - 仅在 usermode 启用时生效
        - 如果移走的车原本在 L1 → 检查是否有其他空闲车已在 L1
        - 如果没有空闲车在 L1 → 派最近的一部空闲车去 L1
        """
        if not self._usermode:
            return

        building = self.config.get('building', {})
        main_floor = building.get('min_floor', 1)  # 主楼层，默认 L1

        # 检查是否已有空闲车在主楼层
        has_idle_at_main = False
        candidates = []

        for cid in self.car_ids:
            if cid == moving_car_id:
                continue
            car = self.cars[cid]
            if (car.state == CarState.READY
                    and car.direction == Direction.IDLE
                    and car.door_state == DoorState.CLOSED
                    and car.position is not None
                    and not self.manual_mode.get(cid, False)):
                if car.position == main_floor:
                    has_idle_at_main = True
                    break
                else:
                    candidates.append((abs(car.position - main_floor), cid))

        if has_idle_at_main or not candidates:
            return  # 已有车在 L1 待命，或无空闲车

        # 选最近的车派去 L1
        candidates.sort()
        _, target_cid = candidates[0]
        print(f'[auto_park] car{moving_car_id} 离开 → car{target_cid} 自动回 L{main_floor}')
        await self.call_internal(main_floor, car_id=target_cid, origin='auto_park')

    def _make_on_action_done(self, car_id: int):
        async def _on_action_done(last_action: Action) -> None:
            await self._on_action_done(car_id, last_action)
        return _on_action_done

    async def _on_action_done(self, car_id: int, last_action: Action) -> None:
        """
        Action 完成事件分发器

        只做算法编排 (mid-level):
        - MOVE 完成 → 清 pending, 外召到站开门
        - INITIALIZE 完成 → 启动方向运行
        上层应用逻辑（开关门后自动流程等）通过事件监听机制由外部模块接入。
        """
        if last_action is None:
            await self._tick(car_id)
            return

        advanced = await self._handle_algorithm_state_change(car_id, last_action)

        # H2 钩子:关门完成后更新重量状态（必须在大脑决策之前）
        # 否则 PM._on_door_closed dispatch 时读到的是 H1 时刻的过期 weight_state
        if last_action.kind == ActionKind.CLOSE_DOOR:
            await self.weight_manager.on_close_door_completed(car_id)

        # 通知大脑（PassengerManager 需要知道门开/门关完成）
        pm_dispatched = False
        if self.pm is not None:
            try:
                await self.pm.on_action_done(car_id, last_action)
            except Exception as e:
                print(f'[app] car{car_id} PM.on_action_done 异常: {e!r}')

        # ★ 孤儿 pickup 回收：pickup 还在但楼层已不在 pending_calls 里
        # 场景：偷客/改道后 pickup 残留，车不再去那个楼层，灯永远亮。
        # 回收进 _pending_hall_calls → 空闲车重新派过去。
        if self.pm is not None:
            for cid in self.car_ids:
                car = self.cars[cid]
                if car.state != CarState.READY:
                    continue
                my_pending = self.pending_calls.get(cid, [])
                for (floor, direction), active in list(
                        self.pm._pickup_active[cid].items()):
                    if not active:
                        continue
                    if floor not in my_pending:
                        # 孤儿：回收
                        self.pm._pickup_active[cid].pop((floor, direction), None)
                        self.pm._pending_hall_calls.add((floor, direction))
                        self.pm._log_pickup('orphan', cid, floor, direction, 'recycled')
                        self.pm._log_pending('add', floor, direction, 'orphan_recycled')
                        await self.set_hall_indicator(floor, direction, True)

        # ★ 车变空闲时：扫描所有空闲车派 pending 外召
        if self.pm is not None and self.pm._pending_hall_calls:
            for cid in self.car_ids:
                if not self.pm._pending_hall_calls:
                    break
                car = self.cars[cid]
                if (car.state == CarState.READY
                        and car.direction == Direction.IDLE
                        and car.door_state == DoorState.CLOSED
                        and car.position is not None
                        and self.executors[cid].current_action is None
                        and not self.manual_mode.get(cid, False)):
                    # ★ 跳过有内召的车：车内有乘客要去某层，先送达再说
                    origins = self.pending_call_origin.get(cid, {})
                    has_internal = any(
                        origins.get(f) == 'internal'
                        for f in self.pending_calls.get(cid, []))
                    if has_internal:
                        continue
                    # ★ 跳过临界/超重车：weight_state>=1 不响应外呼
                    if getattr(car, 'weight_state', 0) >= 1:
                        continue
                    await self.pm._try_dispatch_pending_hall_calls(cid)

        if not advanced:
            # CLOSE_DOOR → PM._on_door_closed 已通过 call_internal → _tick 派车
            # OPEN_DOOR → PM._on_door_opened 只调度 cron，不派车，仍需 _tick
            # 避免双重 _tick 导致重复 MOVE 入队（第二个 MOVE 到站开门后静默失败）
            if last_action.kind != ActionKind.CLOSE_DOOR:
                await self._tick(car_id)

        # ★ 兜底：车变空闲且门关着 + 无请求 → 启动 HP timer
        # 场景：origin=internal 到站不开门 / _on_door_closed 因 pending 非空没调度 HP
        # 这两种情况 _hp_timer 永远不会启动，灯永远亮
        if (self.pm is not None
                and self.executors[car_id].current_action is None
                and self.cars[car_id].door_state == DoorState.CLOSED
                and self.cars[car_id].state == CarState.READY):
            jn = self.pm._human_presence_job_name(car_id)
            # 没在 pending_calls / pending_hall_calls / pickup_active
            no_pending = (not self.pending_calls[car_id]
                          and not self.pm._pending_hall_calls
                          and not any(self.pm._pickup_active.get(car_id, {}).values()))
            if no_pending:
                # 检查 cron 是否已经 schedule
                scheduled = any(j.name == jn for j in self.cron._jobs.values())
                if not scheduled:
                    await self.pm._start_human_presence_timer(car_id)
                    self._log_ui(f'[hp_fallback] car{car_id} 没收到 door_close,主动启动 HP timer')

        # ★ 外呼灯一致性校验：确保灯亮 = 有活跃外呼
        if self.pm is not None:
            try:
                await self.pm.reconcile_hall_indicators()
            except Exception:
                pass

        # WebSocket 广播：每次 action 完成推一次全车状态
        try:
            from web import ws_broadcast
            await ws_broadcast('car_state', {
                str(car_id): self.car_state_dict(car_id)
                for car_id in self.car_ids
            })
        except Exception:
            pass  # Web 服务未启动或客户端断开，静默忽略

    async def _handle_algorithm_state_change(
        self, car_id: int, last_action: Action
    ) -> bool:
        """
        算法状态转换 (mid-level)

        只关心"算法编排后的电梯下一步该做什么":
        - MOVE 完成 → 清 pending + 外召开门
        - INITIALIZE 完成 → 启动方向运行
        返回 True 表示已接管下一步动作(避免上层 _tick 再插一手)。
        """
        kind = last_action.kind

        if kind in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            pos = self.cars[car_id].position
            target = self.cars[car_id].target_floor
            if target is not None and pos == target:
                self.pending_calls[car_id] = [
                    c for c in self.pending_calls[car_id] if c != target
                ]
                origin = self.pending_call_origin[car_id].pop(target, 'internal')
                self.cars[car_id].target_floor = None
                self.cars[car_id].last_dispatch_direction = Direction.IDLE  # 到站后清除方向
                # 外召到站 → 开门(内召不碰门)
                if origin == 'hall':
                    await self.action_queues[car_id].put(
                        Action(ActionKind.OPEN_DOOR))
                    return True
            elif target is not None:
                # pos != target: MOVE 被中断或过冲，仍清理 target_floor 防止残留
                self.cars[car_id].target_floor = None
            return False

        if kind == ActionKind.INITIALIZE:
            target = last_action.floor
            if target is not None and target != self.cars[car_id].position:
                self.cars[car_id].target_floor = target
                dir_action = Action(
                    ActionKind.MOVE_UP if target > self.cars[car_id].position
                    else ActionKind.MOVE_DOWN
                )
                await self.action_queues[car_id].put(dir_action)
                return True
            return False

        # 门动作完成不推 MOVE（安全约束）
        # 开门 → 阻止 _tick（门开着不能跑算法，等手动 /door close 或
        # 未来 passenger_flow 关门后 CLOSE_DOOK 才恢复调度）
        if kind == ActionKind.OPEN_DOOR:
            return True

        # 关门完成：如果 passenger manager 已启用，它已在 pm.on_action_done 中
        # 触发了 _on_door_closed → call_internal → _tick，这里不能再跑 _tick
        # 否则会产生双重 MOVE dispatch
        if kind == ActionKind.CLOSE_DOOR and self.pm is not None:
            return True

        return False

    def _make_on_emergency_stop(self, car_id: int):
        async def on_emergency():
            self.pending_calls[car_id].clear()
            self.cars[car_id].target_floor = None
            self.manual_mode[car_id] = False
            if self.pm is not None:
                await self.pm.on_emergency(car_id)
            print(f'[emergency] car {car_id} 紧急停止')
        return on_emergency

    def _make_on_breach(self, car_id: int):
        """door breach callback — notifies PassengerManager"""
        async def on_breach():
            if self.pm is not None:
                await self.pm.on_breach(car_id)
        return on_breach

    def _make_on_light_curtain(self, car_id: int):
        """light curtain callback — sets human_presence, notifies PM"""
        async def on_light_curtain():
            # 光幕 = 确定有人穿过 → 设为 1
            self.cars[car_id].human_presence = 1
            if self.pm is not None:
                await self.pm.on_light_curtain(car_id)
        return on_light_curtain

    def _make_on_lc_close(self, car_id: int):
        """关门中光幕触发 → 亮故障灯警示乘客"""
        async def on_lc_close():
            await self.ui[car_id].set_fault(True)
        return on_lc_close

    def _make_on_open_done_fault_off(self, car_id: int):
        """开门到位 → 熄故障灯"""
        async def on_open_done():
            await self.ui[car_id].set_fault(False)
        return on_open_done

    # ===== 高层 API（给大脑/console 用） =====

    async def set_predicted_direction_indicator(self, car_id: int,
                                                 target_floor: int) -> None:
        """派车成功后立即亮起预测方向灯（不等 MOVE 动作启动）

        大脑(乘客意图)→ 小脑(本方法)→ 脑干(motor IO)
        已在目标层或位置未知时静默跳过；IO 异常吞掉不影响派车/开门。
        """
        car = self.cars[car_id]
        pos = car.position
        if pos is None or pos == target_floor:
            return
        direction = 'up' if target_floor > pos else 'down'
        try:
            await self.executors[car_id].motor.set_direction_indicator(direction)
        except Exception:
            pass

    # ===== 高层 API（给 console 用） =====

    async def call_internal(self, floor: int, car_id: int | None = None,
                            origin: str = 'internal') -> bool:
        cid = car_id if car_id is not None else self.current_car_id
        # ★ 发车前再次校验门状态:必须是 CLOSED 或 (CLOSING + door_close_done=1)
        # 防止 PM._select_car_for_hall_call 选中后状态在间隙内变化导致"关门未完就发车"
        car = self.cars[cid]
        if car.door_state == DoorState.CLOSED:
            pass
        elif car.door_state == DoorState.CLOSING:
            try:
                close_done_addr = self.mapper.addr_input(
                    'door_close_done', cid)
                if self.io.get_input(close_done_addr) != 1:
                    self._log_ui(f'[call_internal] car{cid} rejected: door=closing, close_done≠1')
                    return False
            except KeyError:
                self._log_ui(f'[call_internal] car{cid} rejected: door=closing, no close_done addr')
                return False
        else:
            self._log_ui(f'[call_internal] car{cid} rejected: door={car.door_state.value}')
            return False  # OPEN/OPENING:拒绝,避免乘客还在上下时车跑掉
        if floor in self.pending_calls[cid]:
            self._log_ui(f'[call_internal] car{cid} rejected: floor L{floor} already pending={self.pending_calls[cid]}')
            return False
        if self.cars[cid].position == floor and not self.pending_calls[cid]:
            return False
        self.pending_calls[cid].append(floor)
        # 不覆盖已设的 origin（如 _on_door_closed 预填了 'hall'）
        self.pending_call_origin[cid].setdefault(floor, origin)
        if self.cars[cid].target_floor is None:
            self.cars[cid].target_floor = floor
        await self._tick(cid)
        return True

    def call_rejection_reason(self, floor: int, car_id: int) -> str | None:
        """镜像 call_internal 的拒绝条件,返回具体原因;None 表示可接受

        用于 REPL 给用户具体原因,避免 '门未关或其他原因' 这种模糊提示。
        拒绝条件(与 call_internal 严格对齐,顺序一致):
            1. 门未关
            2. 楼层已在 pending_calls
            3. 车已在该楼层且无 pending 任务
        """
        car = self.cars[car_id]
        if car.door_state != DoorState.CLOSED:
            return f'门未关 ({car.door_state.value})'
        if floor in self.pending_calls[car_id]:
            return f'L{floor} 已在召唤队列'
        if car.position == floor and not self.pending_calls[car_id]:
            return f'已在 L{floor}'
        return None

    async def change_internal(self, floor: int, car_id: int) -> str:
        """中途更改目的地

        MOVE_UP/MOVE_DOWN 运行时，将目标改为一个更近的楼层（缩短行程）。
        如果电梯已经过了刹得住的位置，则拒绝。

        Returns:
            'accepted'  — 已接受：清空 pending_calls，改 target_floor 为 floor
            'rejected'  — 拒绝：无法在当前位置刹停到目标楼层
            'not_running' — 电梯当前未在移动
        """
        cid = car_id
        car = self.cars[cid]
        exe = self.executors[cid]
        building = self.config['building']

        # 0. 楼层范围检查
        if not (building['min_floor'] <= floor <= building['max_floor']):
            return 'rejected'

        # 1. 必须正在运行 MOVE
        action = exe.current_action
        if action is None or action.kind not in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            return 'not_running'

        pos = car.position
        target = car.target_floor
        if pos is None or target is None:
            return 'not_running'

        # 2. 方向判断 + 缩短检查 + 刹车距离检查
        if action.kind == ActionKind.MOVE_UP:
            # 上行：change 必须在当前位置至少 1 层之上（留刹车指令下发时间）、原目标之下
            if not (pos + 1 < floor < target):
                return 'rejected'
        else:  # MOVE_DOWN
            # 下行：change 必须在当前位置至少 1 层之下、原目标之上
            if not (pos - 1 > floor > target):
                return 'rejected'

        # 3. 接受：改目标 + 清队列
        car.target_floor = floor
        self.pending_calls[cid].clear()
        return 'accepted'

    async def fireman(self, floor: int, car_id: int) -> dict:
        """救火命令：找到最近可平层停靠的楼层，先停车再换向

        核心原则：不可中途倒车——必须先在一个合法楼层完成平层停靠后，
        再改变运行方向。直接倒车会破坏楼层计数器。

        Returns:
            {'status': 'called'|'noop'|'changed'|'waypoint'|'queued',
             'waypoint': int|None}
        """
        cid = car_id
        car = self.cars[cid]
        exe = self.executors[cid]
        building = self.config['building']

        # 0. 楼层范围检查
        if not (building['min_floor'] <= floor <= building['max_floor']):
            return {'status': 'invalid'}
        if car.position is None or car.state != CarState.READY:
            return {'status': 'invalid'}

        action = exe.current_action
        pos = car.position
        target = car.target_floor

        # 1. 不在 MOVE → call
        if action is None or action.kind not in (ActionKind.MOVE_UP, ActionKind.MOVE_DOWN):
            await self.call_internal(floor, car_id=cid)
            return {'status': 'called'}

        # 2. 已在去目标层的路上
        if floor == target:
            return {'status': 'noop'}

        if action.kind == ActionKind.MOVE_UP:
            # === 上行 ===
            # a. 顺向且刹得住：直接 change
            if pos + 1 < floor <= target:
                await self.change_internal(floor, car_id=cid)
                return {'status': 'changed'}

            # b. 刹不住 / 逆向 / 延长 → 找 waypoint
            waypoint = pos + 2
            if waypoint < target:
                # 有中间站可停靠 → 先平层到 waypoint，再倒车
                await self.change_internal(waypoint, car_id=cid)
                self.pending_calls[cid].append(floor)
                return {'status': 'waypoint', 'waypoint': waypoint}

            # 无中间站（如 pos=9→target=10）→ 等当前 MOVE 完再 call
            self.pending_calls[cid].clear()
            self.pending_calls[cid].append(floor)
            return {'status': 'queued'}

        else:
            # === 下行 ===
            # a. 顺向且刹得住：直接 change
            if pos - 1 > floor >= target:
                await self.change_internal(floor, car_id=cid)
                return {'status': 'changed'}

            # b. 刹不住 / 逆向 / 延长 → 找 waypoint
            waypoint = pos - 2
            if waypoint > target:
                # 有中间站可停靠 → 先平层到 waypoint，再倒车
                await self.change_internal(waypoint, car_id=cid)
                self.pending_calls[cid].append(floor)
                return {'status': 'waypoint', 'waypoint': waypoint}

            # 无中间站 → 等当前 MOVE 完再 call
            self.pending_calls[cid].clear()
            self.pending_calls[cid].append(floor)
            return {'status': 'queued'}

    async def reset(self, direction: str | None = None,
                    target_floor: int | None = None,
                    car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        from .player import FaultFlags
        self.cars[cid].state = CarState.UNKNOWN
        self.cars[cid].position = None
        self.cars[cid].target_floor = None
        self.cars[cid].fault = FaultFlags()
        # 防御性重置门状态:防止上一次会话残留的 OPEN/OPENING/CLOSING 导致
        # 下一次 init 后 dispatch 失败（门开着不派新外召）。
        self.cars[cid].door_state = DoorState.CLOSED
        self.pending_calls[cid].clear()
        # 同步清 executor 的 init 残留（双重保险：_start_action 也清了）
        self.executors[cid]._init_reverse_mode = False
        self.executors[cid]._init_perfect_leveling_active = False
        self.executors[cid]._init_base_segment_done = False
        # 清 executor 瞬态状态(复用 _emergency_stop 的清理模式,executor.py:401-411)
        # 不清 paused / _station_seek_enabled / manual_mode —— 这些是用户状态
        exe = self.executors[cid]
        exe.current_action = None
        exe.waiting_sensor = None
        exe.decel_state = ''
        exe._last_level_up = 0
        exe._last_level_down = 0
        exe._level_seek_active = False
        exe._level_seek_skip_next = False
        exe._level_correct_in_progress = False
        exe._auto_seek_active = False
        self._door_busy[cid] = False
        # PM cron 由 self.pm.reset(cid) 在 _reset_state 中取消
        # 此处只取消小脑的兜底 cron
        await self.cron.cancel(f'door_timeout_{cid}_open')
        await self.cron.cancel(f'door_timeout_{cid}_close')
        self.pending_call_origin[cid].clear()
        # 重置大脑状态
        if self.pm is not None:
            await self.pm.reset(cid)
        self.cars[cid].human_presence = -1
        # 重置 UI 状态(逻辑状态清零 + 同步到 IO)
        self.cars[cid].ui = IndicatorState()
        await self.ui[cid].sync_to_io()
        # 清空动作队列:避免旧 MOVE 在新 INITIALIZE _start_action 覆盖前污染状态
        while not self.action_queues[cid].empty():
            try:
                self.action_queues[cid].get_nowait()
            except asyncio.QueueEmpty:
                break
        if direction:
            self.executors[cid].init_direction = direction
        tf = target_floor if target_floor is not None else 1
        action = Action(ActionKind.INITIALIZE, floor=tf)
        await self.action_queues[cid].put(action)

    def _get_car_init_config(self, car_id: int) -> tuple[str, int]:
        """获取指定轿厢的初始化配置（方向 + 目标层）

        优先读 per_car_init，fallback 到全局 initialization_direction 和 L1。
        """
        per_car = self.config.get('elevator', {}).get('per_car_init', {})
        cfg = per_car.get(str(car_id), {})
        direction = cfg.get('direction') or self.config.get('elevator', {}).get('initialization_direction', 'down')
        target_floor = cfg.get('target_floor', 1)
        return direction, target_floor

    async def reload(self) -> None:
        self._load_config()
        self.mapper.reload()
        self.display.reload()
        building = self.config['building']
        io2http = self.config['io2http']
        self.io._tick_interval = max(0.01, io2http.get('tick_interval_ms', 100) / 1000.0)
        for cid in self.car_ids:
            self.executors[cid].top_base_floor = building['top_base_floor']
            self.executors[cid].bottom_base_floor = building['bottom_base_floor']
            self.executors[cid].init_direction = self.config['elevator']['initialization_direction']
        # 站点吸附开关 reload 同步
        station_seek_enabled = self.config['elevator'].get('station_seek', False)
        for cid in self.car_ids:
            self.executors[cid].set_station_seek(station_seek_enabled)
        # slow_brake reload 同步
        slow_brake = self.config['elevator'].get('slow_brake', 0)
        for cid in self.car_ids:
            self.executors[cid].motor.slow_brake_level = slow_brake
        # ui_config reload 同步（熄灯延时/关门延时等）
        if self.pm is not None:
            self.pm._reload_ui_config()
        print(f'[reload] config reloaded: init_dir={self.config["elevator"]["initialization_direction"]}, '
              f'station_seek={station_seek_enabled}, slow_brake={slow_brake}')

    def _save_elevator_config(self, key: str, value: Any) -> None:
        """将 elevator.<key> 写回 config.yaml（保留注释和格式）

        使用正则匹配 `key: <旧值>` 行并替换为 `key: <新值>`。
        不依赖 yaml.safe_dump（会丢失注释）。
        """
        import re
        text = self.config_path.read_text(encoding='utf-8')
        # 匹配 elevator 段内的 key: value（缩进 2 空格）
        pattern = rf'^(\s+{re.escape(key)}:\s*)(.+)$'
        new_text, n = re.subn(pattern, rf'\g<1>{value}', text, count=1, flags=re.MULTILINE)
        if n == 0:
            # 如果 key 不存在，追加到 elevator 段末尾（不常见）
            # 找 elevator: 段最后一个非注释行后追加
            pass
        self.config_path.write_text(new_text, encoding='utf-8')
        # 同步更新内存中的 config
        self.config['elevator'][key] = value

    async def set_station_seek(self, enabled: bool) -> dict[str, int]:
        """切换站点吸附开关（高层 API，console 用）

        对所有轿厢:打开 flag。
        开启时,空闲且传感器在 (0,0) 的车入队 INITIALIZE down 1(自动寻站);
        空闲且在平层位的车立即激活 hold。
        忙车只 set flag,等下次 _arrive_and_brake 兜底。

        返回:{auto_seek_count, activate_count, skipped_count} 给 console 展示。
        """
        auto_seek_count = 0
        activate_count = 0
        skipped_count = 0

        for cid in self.car_ids:
            exe = self.executors[cid]
            # 先设置 flag（executor 内部清/保留激活态）
            exe.set_station_seek(enabled)

            if not enabled:
                continue

            # 跳过手动模式(executor.paused=True)的车
            if exe.paused:
                skipped_count += 1
                continue

            # 正在跑的车:只 set flag,等 _arrive_and_brake
            if exe.current_action is not None:
                skipped_count += 1
                continue

            # 空闲车:读 level 传感器判断是否需要 auto-seek
            try:
                up_addr = self.mapper.addr_input('level_up', cid)
                dn_addr = self.mapper.addr_input('level_down', cid)
            except KeyError:
                # 没有 level 信号（异常配置）→ 仅激活 hold,让下次 IO 事件触发检查
                exe._level_seek_active = True
                _fire_and_forget(exe._level_seek_check(), name=f'level_seek_car{cid}')
                activate_count += 1
                continue

            up_now = self.io.get_input(up_addr)
            dn_now = self.io.get_input(dn_addr)

            if up_now == 0 and dn_now == 0:
                # 车散在楼层之间 → auto-seek: 直接下跑找最近一个 (↑1↓1)
                # 不入队 INITIALIZE（不需要反向、不需要计数到 L1）
                await exe.start_auto_seek_down()
                auto_seek_count += 1
            else:
                # 已在平层区(含偏离 1,0 / 0,1),立即激活 hold
                exe._level_seek_active = True
                _fire_and_forget(exe._level_seek_check(), name=f'level_seek_car{cid}')
                activate_count += 1

        return {
            'auto_seek_count': auto_seek_count,
            'activate_count': activate_count,
            'skipped_count': skipped_count,
        }

    def station_seek_enabled(self) -> bool:
        """是否有任意一部车的吸附开启"""
        return any(
            self.executors[cid].is_station_seek_enabled()
            for cid in self.car_ids
        )

    # ===== 用户模式（usermode） =====

    @property
    def usermode_enabled(self) -> bool:
        return self._usermode

    async def set_usermode(self, enabled: bool,
                            cars: list[int] | None = None) -> dict:
        """切换用户模式

        启用时：验证所有轿厢已初始化（state=READY + position 非空）
               → 设置 ready 信号为 1，PLC 认为电梯准备就绪
        禁用时：设置 ready 信号为 0

        Args:
            enabled: True=启用, False=关闭
            cars: 限定检查的轿厢范围;None=全部车(默认,严格模式,任一未就绪即拒绝)

        Returns:
            {'enabled': bool,
             'blocked': list[int],         # 未就绪车 id 列表
             'enabled_cars': list[int]}    # 本次启用成功的车 id(仅启用时)
        """
        result: dict[str, object] = {
            'enabled': enabled,
            'blocked': [],
            'enabled_cars': [],
        }
        target_cars = cars if cars is not None else self.car_ids

        if enabled:
            blocked: list[int] = []
            ready: list[int] = []
            for cid in target_cars:
                car = self.cars[cid]
                if car.state != CarState.READY or car.position is None:
                    blocked.append(cid)
                else:
                    ready.append(cid)
            result['blocked'] = blocked
            result['enabled_cars'] = ready
            # 严格模式 (cars=None): 任一车未就绪即整体拒绝
            # partial 模式 (cars=[...]): console 已预过滤为已就绪车,blocked 必为空
            if cars is None and blocked:
                return result  # 严格模式拒绝,不设 ready
            if not ready:
                return result  # 没有任何就绪车,拒绝
            self._usermode = True
            if self.pm is not None:
                if (not hasattr(self.pm, '_brain_tick_task')
                        or self.pm._brain_tick_task is None):
                    interval = self.config.get('elevator', {}).get(
                        'brain_tick_interval_ms', 2000)
                    self.pm.start_brain_tick(interval_ms=interval)
            try:
                ready_addr = self.mapper.addr_output('ready', 0)
                await self.io.set(ready_addr, 1)
            except KeyError:
                pass
        else:
            self._usermode = False
            try:
                ready_addr = self.mapper.addr_output('ready', 0)
                await self.io.set(ready_addr, 0)
            except KeyError:
                pass

        # WS 推送：usermode 变化（前端 meta 栏实时更新）
        try:
            from web import ws_broadcast
            await ws_broadcast('system_event', {
                'type': 'usermode',
                'enabled': self._usermode,
                'cars': list(self._usermode_active_cars) if hasattr(self, '_usermode_active_cars') else [],
            })
        except Exception:
            pass

        return result

    async def manual_batch(self, direction: Direction | None,
                           high_speed: bool, car_ids: list[int]) -> None:
        """批量手动方向"""
        for cid in car_ids:
            self.manual_mode[cid] = True
            car = self.cars[cid]
            # 进入手动方向前先释放刹车 —— motor.start 不操作 brake_* 信号,
            # 若残留 station_seek/反冲 hold_stop 的 (1,1,1) 会导致"边开电机边锁死"
            # 手动模式下不需要保留任何"刹车保证"语义,每次都释放最安全。
            if direction is not None:
                await self.executors[cid].motor.release_brakes()
            if direction == Direction.UP:
                if car.direction == Direction.UP and car.manual_speed == high_speed:
                    continue
                car.direction = Direction.UP
                car.manual_speed = high_speed
                await self.executors[cid].motor.start(high_speed=high_speed, direction='up')
            elif direction == Direction.DOWN:
                if car.direction == Direction.DOWN and car.manual_speed == high_speed:
                    continue
                car.direction = Direction.DOWN
                car.manual_speed = high_speed
                await self.executors[cid].motor.start(high_speed=high_speed, direction='down')
            else:  # stop
                if car.direction == Direction.IDLE and car.manual_speed is False:
                    continue
                await self.executors[cid].motor.stop()
                car.direction = Direction.IDLE
                car.manual_speed = False

    async def manual_brake_batch(self, level: int,
                                 car_ids: list[int]) -> None:
        """批量刹车"""
        for cid in car_ids:
            await self.executors[cid].motor.set_brake_level(level)

    async def manual_up(self, high_speed: bool = True,
                        car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = True
        car = self.cars[cid]
        if car.direction == Direction.UP and car.manual_speed == high_speed:
            return
        # 释放残留刹车 —— motor.start 不动 brake_*,必须先释放避免"边开电机边锁死"
        await self.executors[cid].motor.release_brakes()
        car.direction = Direction.UP
        car.manual_speed = high_speed
        await self.executors[cid].motor.start(high_speed=high_speed, direction='up')

    async def manual_down(self, high_speed: bool = True,
                          car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = True
        car = self.cars[cid]
        if car.direction == Direction.DOWN and car.manual_speed == high_speed:
            return
        # 释放残留刹车 —— motor.start 不动 brake_*,必须先释放避免"边开电机边锁死"
        await self.executors[cid].motor.release_brakes()
        car.direction = Direction.DOWN
        car.manual_speed = high_speed
        await self.executors[cid].motor.start(high_speed=high_speed, direction='down')

    async def manual_stop(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        car = self.cars[cid]
        if car.direction == Direction.IDLE and car.manual_speed is False:
            return
        await self.executors[cid].motor.stop()
        car.direction = Direction.IDLE
        car.manual_speed = False

    async def manual_brake(self, level: int | None = None,
                           car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        exe = self.executors[cid]
        if level is None:
            level = exe.manual_brake_level
        else:
            exe.manual_brake_level = level
        if exe.manual_current_brake_state == level:
            return
        exe.manual_current_brake_state = level
        await exe.motor.set_brake_level(level)

    async def manual_emergency_stop(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = False
        await self.executors[cid]._emergency_stop(reason='manual_e_stop')

    async def async_stop(self, car_id: int | None = None) -> None:
        """emergency stop + forget position / state"""
        cid = car_id if car_id is not None else self.current_car_id
        self.manual_mode[cid] = False
        self.pending_calls[cid].clear()
        self.cars[cid].target_floor = None
        await self.executors[cid]._emergency_stop(reason='async_stop')
        self.cars[cid].state = CarState.UNKNOWN
        self.cars[cid].position = None
        # 清空 action 队列
        q = self.action_queues[cid]
        while not q.empty():
            try:
                q.get_nowait()
            except Exception:
                break

    async def emergency_escape_all(self) -> None:
        """火警模式：所有轿厢就近平层停车→亮故障灯→忘掉楼层→开门"""
        for cid in self.car_ids:
            car = self.cars[cid]
            # 紧急停车 + 清 pending calls
            self.manual_mode[cid] = False
            self.pending_calls[cid].clear()
            car.target_floor = None
            await self.executors[cid]._emergency_stop(reason='escape')
            car.state = CarState.UNKNOWN
            car.position = None
            q = self.action_queues[cid]
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break
            # 如果车在楼层之间（无平层信号），慢速寻平层
            exe = self.executors[cid]
            try:
                up_addr = self.mapper.addr_input('level_up', cid)
                dn_addr = self.mapper.addr_input('level_down', cid)
            except KeyError:
                up_addr = dn_addr = None
            if up_addr is not None:
                up = self.io.get_input(up_addr)
                dn = self.io.get_input(dn_addr)
                if not (up == 1 and dn == 1):
                    self._log(f'[escape] car{cid} 寻平层...')
                    dir_str = 'up' if car.direction != Direction.DOWN else 'down'
                    await exe.motor.release_brakes()
                    await exe.motor.start(high_speed=False, direction=dir_str)
                    deadline = asyncio.get_event_loop().time() + 10
                    found = False
                    while asyncio.get_event_loop().time() < deadline:
                        await asyncio.sleep(0.1)
                        up = self.io.get_input(up_addr)
                        dn = self.io.get_input(dn_addr)
                        if up == 1 and dn == 1:
                            found = True
                            break
                    await exe.motor.hold_stop()
                    if not found:
                        self._log(f'[escape] car{cid} 寻平层超时')
            # 亮故障灯
            try:
                f_addr = self.mapper.addr_output('fault_indicator', cid)
                await self.io.set(f_addr, 1)
            except KeyError:
                pass
            # 开门
            await exe.door.open()
            await exe.door.wait_done()
            car.door_state = DoorState.OPEN
            self._log(f'[escape] car{cid} 已停车,开门,点亮故障灯')

    async def manual_auto(self, car_id: int | None = None) -> None:
        cid = car_id if car_id is not None else self.current_car_id
        await self.manual_brake(0, car_id=cid)
        await self.manual_stop(car_id=cid)
        self.manual_mode[cid] = False

    async def clear_outputs(self) -> None:
        for cid in self.car_ids:
            writes: dict[str, int] = {}
            for sig in self.mapper.all_output_signals(cid):
                if sig == 'ready':
                    continue
                try:
                    db_addr = self.mapper.addr_output(sig, cid)
                    writes[db_addr] = 0
                except KeyError:
                    continue
            await self.io_write[cid].set_many(writes)
        # 全局共享输出(ready 等)用 self.io 清
        global_writes: dict[str, int] = {}
        for sig in self.mapper.all_output_signals(0):
            try:
                db_addr = self.mapper.addr_output(sig, 0)
                global_writes[db_addr] = 0
            except KeyError:
                continue
        if global_writes:
            await self.io.set_many(global_writes)
        # 清空逻辑状态(UI + hall indicator)
        for cid in self.car_ids:
            self.cars[cid].ui = IndicatorState()
        self._hall_indicator_state.clear()
        print(f'[clear] 所有输出已置零')

    async def set_hall_indicator(self, floor: int, direction: str,
                                  on: bool) -> None:
        """外召按钮指示灯(建筑级信号,不属于任何轿厢)

        Args:
            floor: 楼层号 (up: 1..9, down: 2..10)
            direction: 'up' | 'down'
            on: True=亮, False=灭
        """
        if direction not in ('up', 'down'):
            raise ValueError(
                f"direction 必须是 'up' 或 'down',got {direction!r}"
            )
        max_f = self.config['building']['max_floor']
        min_f = self.config['building']['min_floor']
        if direction == 'up' and not (min_f <= floor < max_f):
            return  # 边界楼层静默跳过（如10楼没有上行）
        if direction == 'down' and not (min_f + 1 <= floor <= max_f):
            return  # 边界楼层静默跳过（如1楼没有下行）
        sig = f'hall_indicator_{direction}_{floor}'
        try:
            addr = self.mapper.addr_output(sig, 0)
        except KeyError:
            return  # 信号未在 io_config 中配置，静默跳过
        self._hall_indicator_state[(floor, direction)] = on
        await self.io.set(addr, 1 if on else 0)
        # 通知外召灯 observer（事件驱动）
        for cb_o in self._hall_light_observers:
            try:
                await cb_o(floor, direction, on)
            except Exception:
                pass

    def hall_indicator_state(self, floor: int, direction: str) -> bool:
        """读当前外召按钮指示灯状态(供 /buttonui toggle 用)"""
        return self._hall_indicator_state.get((floor, direction), False)

    # ===== /door 命令 =====

    def _door_precheck(self, car_id: int, action: str,
                        force: bool) -> dict | None:
        """预检:不通过返回 dict 错误,通过返回 None

        检查项:
            - car_door_lock 信号存在
            - init 检查 (force 跳过)
            - 移动检查 (始终生效,force 也不越过)
            - 门已开/已关 检查 (force 跳过)
        """
        car = self.cars[car_id]
        io = self.io
        mapper = self.mapper

        try:
            car_lock_i = mapper.addr_input('car_door_lock', car_id)
        except KeyError:
            return {
                'status': 'rejected',
                'message': f'car {car_id} io_config 缺 car_door_lock 信号',
            }
        car_door_locked = bool(io.get_input(car_lock_i))

        if not force and car.state != CarState.READY:
            return {
                'status': 'rejected',
                'message': f'car {car_id} 未初始化,需要 force 参数强制执行',
            }
        if car.direction != Direction.IDLE:
            return {
                'status': 'rejected',
                'message': f'car {car_id} 正在移动({car.direction.value}),无法执行门操作',
            }
        if not force:
            if action == 'open' and not car_door_locked:
                # 门正在关 → 允许重开
                if car.door_state == DoorState.CLOSING:
                    pass  # 放行，让 executor 处理重开
                else:
                    return {
                        'status': 'rejected',
                        'message': f'car {car_id} 门已开(car_door_lock=false),无需再开',
                    }
            if action == 'close' and car_door_locked:
                return {
                    'status': 'rejected',
                    'message': f'car {car_id} 门已关好(car_door_lock=true),无需再关',
                }
        return None  # 通过

    async def control_door(self, car_id: int, action: str,
                           force: bool = False) -> dict:
        """控制开门/关门。**非阻塞**,预检后立即返回 dispatched,后台跟踪完成。

        Args:
            car_id: 轿厢 ID
            action: 'open' | 'close'
            force: 跳过部分预检,直接拉 relay 立即返回

        Returns:
            dict {
                'status': 'dispatched' | 'rejected' | 'busy' | 'force_done',
                'message': str,
            }

            - 'dispatched': 已派发 action + 后台 task 跟踪,REPL 不阻塞
                           完成 / 错层 由后台 task / /debug show door_status 输出
            - 'force_done': force 模式已拉 relay
            - 'rejected' / 'busy': 命令级错误,命令立即打印
        """
        if car_id not in self.car_ids:
            return {'status': 'rejected', 'message': f'无效轿厢 ID: {car_id}'}
        if action not in ('open', 'close'):
            return {'status': 'rejected', 'message': f"action 必须是 'open' 或 'close', got {action!r}"}

        door = self.executors[car_id].door
        mapper = self.mapper
        io = self.io

        # 1. 预检 (同步,极快)
        precheck_result = self._door_precheck(car_id, action, force)
        if precheck_result is not None:
            return precheck_result

        # 2. force: 直接拉 relay + 立即返回
        if force:
            if action == 'open':
                await door.open()
            else:
                await door.close()
            return {
                'status': 'force_done',
                'message': f'car {car_id} force 模式:已拉 {action} relay(不等待门锁)',
            }

        # 3. 同轿厢互斥（门关中允许重开 → 立即打断关门 + 释放 busy）
        if self._door_busy[car_id]:
            if action == 'open' and self.cars[car_id].door_state == DoorState.CLOSING:
                # 立即中断关门（executor 的 wait_done 会收到 'cancelled'）
                self.executors[car_id].door.cancel_for_reopen()
                self._door_busy[car_id] = False
            else:
                return {
                    'status': 'busy',
                    'message': f'car {car_id} 门动作进行中,请等待完成',
                }
        self._door_busy[car_id] = True

        # 4. 注册 listener (同步,在 dispatch 前,防止 race)
        floor_lock_i: dict[int, str] = {}
        for f in range(1, 11):
            try:
                floor_lock_i[f] = mapper.addr_input(f'floor_door_lock_{f}', car_id)
            except KeyError:
                pass

        door_done_signal = 'door_open_done' if action == 'open' else 'door_close_done'
        try:
            door_done_i = mapper.addr_input(door_done_signal, car_id)
        except KeyError:
            self._door_busy[car_id] = False
            return {
                'status': 'rejected',
                'message': f'car {car_id} io_config 缺 {door_done_signal} 信号',
            }

        done_event = asyncio.Event()
        wrong_floor: list[int] = []
        car = self.cars[car_id]
        pos_at_dispatch = car.position

        async def listener(event: IOEvent) -> None:
            if event.i_addr == door_done_i and event.bit == 1:
                done_event.set()
            elif event.i_addr in floor_lock_i.values():
                if event.bit == 0:
                    f = next(
                        fl for fl, addr in floor_lock_i.items()
                        if addr == event.i_addr
                    )
                    if f != pos_at_dispatch:
                        wrong_floor.append(f)

        io.add_listener(listener)

        # 5. 派发 action
        try:
            kind = ActionKind.OPEN_DOOR if action == 'open' else ActionKind.CLOSE_DOOR
            await self.action_queues[car_id].put(Action(kind))
        except Exception:
            # 入队失败也清理
            io.remove_listener(listener)
            self._door_busy[car_id] = False
            raise

        # 6. 后台 task 跟踪完成 + 错层检测
        _fire_and_forget(self._door_track_completion(
            car_id=car_id,
            action=action,
            listener=listener,
            done_event=done_event,
            wrong_floor=wrong_floor,
        ), name=f'door_track_car{car_id}')

        # 7. cron 兜底:PLC 异常不发 done 信号时,timeout 秒后强制释放 mutex
        # 无 sleep / wait_for,完全由 cron 事件循环驱动
        timeout = self.config.get('elevator', {}).get(
            'door_complete_timeout', 8)
        job_name = f'door_timeout_{car_id}_{action}'

        async def _timeout_cb():
            await self._door_timeout_callback(
                car_id, action, listener, done_event)

        try:
            await self.cron.schedule(CronJob(
                name=job_name,
                trigger_time=time.monotonic() + timeout,
                delay=timeout,
                action=_timeout_cb,
            ))
        except Exception:
            # 兜底调度失败不应影响主流程(派发已成功,后台 task 仍在跟踪)
            print(f'[car {car_id}] 警告: 门超时兜底 cron 调度失败,超时保护未生效')

        # 8. 立即返回,不阻塞 REPL
        return {
            'status': 'dispatched',
            'message': f'car {car_id} 已派发 {action} 命令,后台跟踪中',
        }

    async def _door_track_completion(
        self,
        car_id: int,
        action: str,
        listener,
        done_event: asyncio.Event,
        wrong_floor: list[int],
    ) -> None:
        """后台跟踪任务:等 door_open_done/close_done + 错层检测 + 释放 mutex

        行为:
            - 错层(仅 open):始终打印 ⚠️ (不需 debug 开关)
            - 成功完成:不打印 (由 /debug show door_status 控制)
            - 释放 mutex + 注销 listener(无论结果)
            - 取消 cron 兜底 job(避免 timeout 误触发)
        """
        try:
            await done_event.wait()
            # 成功路径:取消 cron 兜底(若还没 fire)
            await self.cron.cancel(f'door_timeout_{car_id}_{action}')
            if wrong_floor and action == 'open':
                wf = wrong_floor[0]
                pos = self.cars[car_id].position
                print(
                    f'[car {car_id}] ⚠️  开错楼:car 在 L{pos},'
                    f'但 L{wf} 层门锁打开了'
                )
                print(
                    f'         → 需手动 /door {car_id} close force '
                    f'或 /car {car_id} init down 1 重置'
                )
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self.io.remove_listener(listener)
            except Exception:
                pass
            self._door_busy[car_id] = False

    async def _door_timeout_callback(
        self,
        car_id: int,
        action: str,
        listener,
        done_event: asyncio.Event,
    ) -> None:
        """门动作完成超时兜底(cron 触发,无 sleep / wait)

        PLC 异常不发 door_open_done / door_close_done 时,
        强制释放 _door_busy 防锁死,set done_event 唤醒后台 task。
        """
        if not self._door_busy.get(car_id, False):
            return  # 正常完成路径已释放,no-op
        print(
            f'[car {car_id}] ⚠️  门动作超时(无 door_{action}_done 信号),'
            f'强制释放 mutex'
        )
        print(
            f'         → 需手动检查 PLC 状态,后续 /door 命令可正常执行'
        )
        done_event.set()  # 唤醒后台 task
        try:
            self.io.remove_listener(listener)
        except Exception:
            pass
        self._door_busy[car_id] = False

    def status_snapshot(self, car_id: int | None = None) -> dict[str, Any]:
        cid = car_id if car_id is not None else self.current_car_id
        return {
            'car': self.cars[cid].snapshot(),
            'algorithm': self.algorithm.name,
            'pending_calls': list(self.pending_calls[cid]),
            'action_queue_size': self.action_queues[cid].qsize(),
            'init_direction': self.executors[cid].init_direction,
            'simulate': self.simulate,
            'manual_mode': self.manual_mode[cid],
            'usermode': self._usermode,
        }
