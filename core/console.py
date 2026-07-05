"""
console.py —— REPL 控制台 + 命令实现

所有命令都以 / 开头（MC 风格）。
"""

import asyncio
import sys
import termios
import tty
from pathlib import Path
from typing import Awaitable, Callable

from .app import CAR_IDS, App
from .io_client import IOEvent


HELP_TEXT = """
可用命令:
  /car <id> <action> [args...]   指定轿厢执行命令
    动作: init / call / status / manual / auto
  /clear                         将所有输出位置零（不含 ready 信号）
  /debug show pass_floor         toggle 平层监视（每次经过楼层输出 [DEBUG] pass_floor L<n>）
  /debug show input_change       toggle 输入变化监视（打印变化的 I 点信号名）
  /debug show websocket_connect_status  toggle WebSocket 连接状态监视
  /debug show exec_trace          toggle executor [exec] 执行日志
  /debug show elevator_speed      toggle 速度档位监视（高速/减速/刹车）
  /help                          显示这个帮助
  /reload                        重载全部 config
  /quit                          退出

示例:
  /car 1 init                    1 号梯初始化（完整流程：全速→触 1 限位→减速→完美平层）
  /car 1 init up 3               上行触顶后反向计数到 3 楼
  /car 1 manual                  进入手动控制（方向键控制，ESC 退出）
  /car 1 auto                    切回自动控制
  /car 1 call 5                  1 号梯内召 5 楼
  /car 1 status                  查看 1 号梯状态
  /clear                         清空所有输出

提示:
  Tab 键补全命令
  上下键浏览历史

手动控制按键:
  ↑ ↓ ← →          上下行（低速）
  Shift+↑ ↓        上下行（高速）
  空格             刹车（按当前档位）
  0                释放所有刹车
  1-7              设置刹车档位（7=全刹）
  ESC / q / Ctrl-C 退出手动控制
"""


class Console:
    def __init__(self, app: App) -> None:
        self.app = app
        # 当前选中的 car_id（/car <id> 切换）
        self.current_car_id: int = app.car.car_id
        self._commands: dict[str, Callable[[list[str]], Awaitable[None]]] = {
            'car': self.cmd_car,
            'clear': self.cmd_clear,
            'debug': self.cmd_debug,
            'help': self.cmd_help,
            'reload': self.cmd_reload,
            'quit': self.cmd_quit,
        }
        # debug 监视项状态
        self.pass_floor_monitor_enabled: bool = False
        self._pass_floor_last_perfect: dict[int, bool] = {}
        self._pass_floor_listener_ref = None
        self.input_change_monitor_enabled: bool = False
        self._input_change_listener_ref = None
        self.ws_monitor_enabled: bool = False
        self._ws_monitor_task: asyncio.Task | None = None
        self._last_ws_connected: bool = False
        self.exec_trace_enabled: bool = False
        self.elevator_speed_enabled: bool = False
        self._elevator_speed_task: asyncio.Task | None = None
        self._last_speed_state: dict[int, str] = {}

    def _resolve_car_id(self, args: list[str]) -> int:
        """从参数里提取 car_id（如果有），否则用当前选中的"""
        if args and args[0].isdigit():
            return int(args[0])
        return self.current_car_id

    def _parse_car_list(self, s: str) -> list[int]:
        """'1,2,3' / '1-3' / 'all' / '5' → list[int]，失败抛 ValueError"""
        s = s.strip()
        if s == 'all':
            return list(CAR_IDS)
        # 范围: 1-6
        if '-' in s:
            parts = s.split('-', 1)
            lo, hi = int(parts[0]), int(parts[1])
            return [i for i in range(lo, hi + 1) if i in CAR_IDS]
        # 逗号列表
        ids = [int(x.strip()) for x in s.split(',') if x.strip()]
        for cid in ids:
            if cid not in CAR_IDS:
                raise ValueError(f'无效轿厢 ID: {cid}')
        return ids

    def _parse_token_list(self, s: str, *, cast=int, sep=','):
        """'1,2,3' → [1,2,3]  或 'up,down' → ['up','down']"""
        return [cast(x.strip()) for x in s.split(sep) if x.strip()]

    def _parse_dir_list(self, s: str) -> list[str]:
        """'up' / 'up,down,up' → list[str]"""
        return [x.strip() for x in s.split(',') if x.strip()]

    async def run(self) -> None:
        print('=' * 60)
        print('  Haku_EET  西门子杯电梯控制离散算法  REPL')
        print('=' * 60)
        print('输入 /help 查看命令列表，Tab 补全，上下键历史')
        print()

        # prompt_toolkit 异步 REPL + Tab 补全 + 上下键历史
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.shortcuts import CompleteStyle
        from pathlib import Path

        class HakuCompleter(Completer):
            cmds = sorted([f'/{c}' for c in self._commands])
            commands_with_subs: dict[str, list[str]] = {
                '/car': ['init', 'call', 'status', 'manual', 'auto'],
                '/debug': ['show'],
            }
            sub_sub_args: dict[str, list[str]] = {
                'init': ['up', 'down'],
                'call': ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10'],
                'show': ['pass_floor', 'input_change', 'websocket_connect_status', 'exec_trace', 'elevator_speed'],
            }

            # ===== 通用补全原语 =====

            def _yield_options(self, options, word, append_space=False):
                """从 options 匹配 word 前缀 → 生成 Completion"""
                for opt in options:
                    if word == '' or opt.startswith(word):
                        yield Completion(
                            opt + (' ' if append_space else ''),
                            start_position=-len(word) if word else 0,
                        )

            # ===== 5 个补全函数 =====

            def _complete_cmd(self, word):
                """一级命令补全：完整匹配 + Tab 自动跟空格进二级"""
                yield from self._yield_options(
                    self.cmds, word,
                    append_space=(word in self.commands_with_subs),
                )

            def _complete_car_id(self, word):
                """car_id 补全：支持单数字、逗号列表、all、范围 1-6

                解析 word 里已出现的车号，剩下未出现的作为候补。
                例：
                  ''      → 补 1,2,3,4,5,6
                  '1,'    → 补 2,3,4,5,6
                  '1,2,'  → 补 3,4,5,6
                  '1,2'   → 补 3,4,5,6（last_token='2' 不匹配任何剩余前缀时跳过）
                """
                if word == '':
                    yield from self._yield_options(
                        [str(c) for c in CAR_IDS], word)
                    return

                # 解析已输入的车号
                used = self._parse_used_car_ids(word)
                remaining = [c for c in CAR_IDS if c not in used]

                # word 以 ',' 结尾 → 补剩余车号(单数字)
                if word.endswith(','):
                    yield from self._yield_options(
                        [str(c) for c in remaining], '')
                    return

                # 中间状态(如 '1,2') → 按最后一个 token 前缀匹配
                last_token = word.rsplit(',', 1)[-1]
                yield from self._yield_options(
                    [str(c) for c in remaining], last_token)

            def _parse_used_car_ids(self, word):
                """从 '1,2,3-5,all,6,' 之类的字符串解析已出现的车号集合

                只接受 _parse_car_list 能解析的形态。
                异常 token 忽略（不抛，补全不应让用户输入崩）。
                """
                used: set[int] = set()
                for token in word.split(','):
                    token = token.strip()
                    if not token:
                        continue
                    if token == 'all':
                        used.update(CAR_IDS)
                    elif '-' in token:
                        try:
                            lo_s, hi_s = token.split('-', 1)
                            lo, hi = int(lo_s), int(hi_s)
                            used.update(range(lo, hi + 1))
                        except ValueError:
                            pass
                    elif token.isdigit():
                        used.add(int(token))
                return used

            def _complete_sub_cmd(self, word, subs):
                """二级子命令补全（如 /car 后 init/call/...）"""
                yield from self._yield_options(subs, word)

            def _complete_sub_arg(self, word, sub_cmd):
                """三级参数补全（init→up/down, call→1-10, show→monitor 名）"""
                if sub_cmd in self.sub_sub_args:
                    yield from self._yield_options(
                        self.sub_sub_args[sub_cmd], word)
                    return
                # 未知子命令 → 不补全
                return

            def _complete_init_floor(self, word):
                """四级：/car N init up/down → 楼层号 1-10"""
                yield from self._yield_options(
                    [str(i) for i in range(1, 11)], word)

            # ===== 主调度器 =====

            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith('/'):
                    return

                # 按最后一个空格切: prefix_text + current_word
                last_space = text.rfind(' ')
                prefix_text = text[:last_space + 1] if last_space >= 0 else ''
                current_word = text[last_space + 1:] if last_space >= 0 else text

                # 1. 一级命令（还没输空格）
                if not prefix_text.strip():
                    yield from self._complete_cmd(current_word)
                    return

                parts = prefix_text.split()
                cmd = parts[0]

                # 2. 未知命令 = 不补
                if cmd not in self.commands_with_subs:
                    return

                # 3. /car 路径：car_id → 子命令 → 三级参数 → 四级楼层
                if cmd == '/car':
                    # 3a. 还没 car_id → 补数字
                    if len(parts) < 2:
                        yield from self._complete_car_id(current_word)
                        return
                    # 3b. car_id 还没输完整（不是数字/all/范围/逗号列表）
                    raw = parts[1]
                    if not self._looks_like_car_id(raw):
                        yield from self._complete_car_id(current_word)
                        return
                    # 3c. car_id 已输完 → 补子命令
                    if len(parts) < 3:
                        yield from self._complete_sub_cmd(
                            current_word, self.commands_with_subs[cmd])
                        return
                    sub_cmd = parts[2]
                    # 3d. 子命令已输 → 补三级参数
                    if len(parts) < 4:
                        yield from self._complete_sub_arg(current_word, sub_cmd)
                        return
                    # 3e. /car init up/down → 四级楼层
                    if (sub_cmd == 'init'
                            and parts[3] in ('up', 'down')
                            and len(parts) < 5):
                        yield from self._complete_init_floor(current_word)
                        return
                    return

                # 4. /debug 路径：子命令 → 子命令参数
                if cmd == '/debug':
                    if len(parts) < 2:
                        yield from self._complete_sub_cmd(
                            current_word, self.commands_with_subs[cmd])
                        return
                    yield from self._complete_sub_arg(current_word, parts[1])
                    return

            @staticmethod
            def _looks_like_car_id(raw: str) -> bool:
                """判断 raw 是否像 car_id：纯数字 / all / 含 - / 含 , 且除 ,外都是数字"""
                if raw == 'all' or raw.isdigit() or '-' in raw:
                    return True
                if ',' in raw:
                    # '1,2,3' / '1,' / ',2' 之类
                    stripped = raw.replace(',', '').replace(' ', '')
                    return stripped.isdigit()
                return False

        session = PromptSession(
            completer=HakuCompleter(),
            history=FileHistory(str(Path.home() / '.haku_eet_history')),
            complete_style=CompleteStyle.READLINE_LIKE,
        )

        while True:
            try:
                line = (await session.prompt_async('haku> ')).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if not line.startswith('/'):
                print('命令必须以 / 开头，输入 /help 查看')
                continue
            parts = line[1:].split()
            cmd = parts[0]
            args = parts[1:]
            if cmd == 'quit':
                break
            if cmd not in self._commands:
                print(f'未知命令: /{cmd}，输入 /help 查看')
                continue
            try:
                await self._commands[cmd](args)
            except Exception as e:
                print(f'错误: {e!r}')
            # 让事件循环有机会调度 executor 后台任务和 listener 回调链
            await asyncio.sleep(0.02)
        print()

    async def _run_with_executor_stdin(self) -> None:
        """stdin 不是 tty 时的备用读法（每次通过 executor 同步读一行）"""
        loop = asyncio.get_running_loop()
        while True:
            try:
                print('haku> ', end='', flush=True)
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (KeyboardInterrupt, EOFError):
                print()
                break
            line = line.rstrip('\n').strip()
            if not line:
                if not line and line is None:
                    break
                continue
            if not line.startswith('/'):
                print('命令必须以 / 开头，输入 /help 查看')
                continue
            parts = line[1:].split()
            cmd = parts[0]
            args = parts[1:]
            if cmd == 'quit':
                break
            if cmd not in self._commands:
                print(f'未知命令: /{cmd}，输入 /help 查看')
                continue
            try:
                await self._commands[cmd](args)
            except Exception as e:
                print(f'错误: {e!r}')
            await asyncio.sleep(0.02)
        print()

    # ===== 命令实现 =====

    async def cmd_help(self, args: list[str]) -> None:
        print(HELP_TEXT)

    async def _do_status(self, args: list[str]) -> None:
        requested = self._resolve_car_id(args)
        snap = self.app.status_snapshot(car_id=requested)
        car = snap['car']
        print(f'算法:        {snap["algorithm"]}')
        print(f'模拟模式:    {snap["simulate"]}')
        print(f'初始化方向:  {snap["init_direction"]}')
        print(f'轿厢 ID:     {car["car_id"]}')
        print(f'状态:        {car["state"]}')
        pos = car['position'] if car['position'] is not None else '?'
        print(f'当前位置:    L{pos}')
        print(f'方向:        {car["direction"]}')
        print(f'门状态:      {car["door_state"]}')
        target = car['target_floor']
        print(f'目标楼层:    L{target}' if target else '目标楼层:    -')
        print(f'显示:        {car["display"]}')
        print(f'动作队列:    {snap["action_queue_size"]}')
        mode = '手动' if snap['manual_mode'] else '自动'
        print(f'控制模式:    {mode}')
        print(f'待处理召唤:  {snap["pending_calls"]}')
        f = car['fault']
        active_faults = [
            name for name, val in [
                ('超重', f['overload']),
                ('检修', f['service_mode']),
                ('光幕', f['light_curtain']),
                ('上限位', f['top_limit']),
                ('下限位', f['bottom_limit']),
            ] if val
        ]
        print(f'故障:        {", ".join(active_faults) if active_faults else "无"}')

    async def cmd_cars(self, args: list[str]) -> None:
        print('已启用的轿厢:')
        for cid in CAR_IDS:
            print(f'  - car {cid}')

    async def cmd_car(self, args: list[str]) -> None:
        """
        /car <id> <action> [args...]  切换或路由命令到指定 car
        /car <id>                     切换当前选中的 car（影响后续命令默认值）
        /car 1,2,3,4,5,6 init down 1,2,3,4,5,6  批量 init
        """
        if not args:
            print('用法: /car <id> [init|call|status|manual|auto] [...]')
            return
        try:
            car_ids = self._parse_car_list(args[0])
        except (ValueError, IndexError) as e:
            print(f'参数错误: {e}')
            return
        sub_action = args[1] if len(args) > 1 else None
        sub_args = args[2:]

        # 批量 init / call / manual
        if len(car_ids) > 1:
            if sub_action == 'init':
                await self._do_init_batch(car_ids, sub_args)
            elif sub_action == 'call':
                await self._do_call_batch(car_ids, sub_args)
            elif sub_action == 'manual':
                await self._run_manual(car_ids)
            else:
                print(f'批量命令只支持 init/call/manual，不支持 {sub_action}')
            return

        # 单 car
        car_id = car_ids[0]
        if car_id not in set(CAR_IDS):
            print(f'无效轿厢 ID: {car_id}（有效值: {CAR_IDS}）')
            return

        self.current_car_id = car_id

        if sub_action is None:
            print(f'已切换当前轿厢: car {car_id}')
            return

        if sub_action == 'init':
            await self._do_init(sub_args)
        elif sub_action == 'call':
            await self._do_call(sub_args)
        elif sub_action == 'status':
            await self._do_status(sub_args)
        elif sub_action == 'manual':
            await self._run_manual([car_id])
        elif sub_action == 'auto':
            await self.app.manual_auto(car_id=car_id)
        elif sub_action == 'goto':
            await self._do_call(sub_args)
        else:
            print(f'未知子命令: {sub_action}')

    async def _run_manual(self, car_ids: list[int]) -> None:
        """
        手动控制 raw key loop（前后端彻底解耦）：

        输入 → 高层动作（manual_up/down/stop/brake）
        输出 → 单行 \\r 覆盖的状态栏（永不 print 干扰）

        支持 1 部或全部轿厢：/car 1 manual 或 /car all manual。
        多部时所有操作（方向键、刹车、停止）广播到每部车。
        """
        if not sys.stdin.isatty():
            print('[manual] 当前 stdin 不是 tty，无法捕获方向键。请在真实终端运行。')
            return

        from core.player import Direction, CarState
        first_id = car_ids[0]
        label = f'cars {car_ids}' if len(car_ids) > 1 else f'car {first_id}'
        # 多部手动时以第一部为准显示状态（self.app.car / executor 指向第一部）
        self.current_car_id = first_id

        print()
        print('=' * 50)
        print(f'  {label} 手动控制模式（executor 暂停，可撞限位）')
        print('  ↑ ↓ / ← →   = 上下行（低速）')
        print('  Shift+↑↓    = 上下行（高速）')
        print('  空格         = 立即停 + 刹车')
        print('  数字键 1-7   = 设置刹车档位（0=释放, 7=全刹）')
        print('  ESC / q      = 退出手动控制')
        print(f'  退出会恢复 executor 2 限位保护')
        print('=' * 50)

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        loop = asyncio.get_running_loop()
        brake_level = 0

        # === 松开立即停 ===
        MOVE_RELEASE_TIMEOUT = 0.1  # 100ms
        stop_deadline: float | None = None
        current_motion: tuple[str, bool] | None = None

        # 暂停所有 target executor
        exec_was_paused: dict[int, bool] = {}
        for cid in car_ids:
            exec_was_paused[cid] = self.app.executors[cid].paused
            self.app.executors[cid].paused = True

        def render_status() -> None:
            """单行状态渲染（用 \\r 回到行首覆盖，永不 print 干扰）"""
            car = self.app.car
            pos = f'L{car.position}' if car.position is not None else '?'
            dir_label = {'idle': '·', 'up': '↑', 'down': '↓'}.get(car.direction.value, '?')
            speed_label = ''
            if car.direction == Direction.UP:
                speed_label = ' HIGH' if car.manual_speed else ' LOW'
            elif car.direction == Direction.DOWN:
                speed_label = ' HIGH' if car.manual_speed else ' LOW'
            door_map = {'closed': '关', 'open': '开', 'opening': '开中', 'closing': '关中'}
            door = door_map.get(car.door_state.value, '?')
            faults = []
            if car.fault.overload: faults.append('超重')
            if car.fault.service_mode: faults.append('检修')
            if car.fault.light_curtain: faults.append('光幕')
            if car.fault.top_limit: faults.append('上限位')
            if car.fault.bottom_limit: faults.append('下限位')
            fault_str = ','.join(faults) if faults else '正常'
            line = (
                f'\r[{label}] L={pos}{speed_label} 方向={dir_label} 门={door} '
                f'刹车={brake_level} {fault_str}      '
            )
            sys.stdout.write(line)
            sys.stdout.flush()

        async def transition(direction: str | None, high_speed: bool):
            """按当前运动状态路由。direction=None = 停。广播到所有 target car。"""
            nonlocal current_motion
            target = None if direction is None else (direction, high_speed)
            if current_motion == target:
                return  # 幂等
            current_motion = target
            if len(car_ids) == 1:
                # 单台：走原有逐台方法
                cid = car_ids[0]
                if direction == 'up':
                    await self.app.manual_up(high_speed=high_speed, car_id=cid)
                elif direction == 'down':
                    await self.app.manual_down(high_speed=high_speed, car_id=cid)
                elif direction is None:
                    await self.app.manual_stop(car_id=cid)
            else:
                # 多台：一次 set_many 批量发所有车，避免 HTTP 串行阻塞
                from core.player import Direction as D
                dir_enum = {'up': D.UP, 'down': D.DOWN, None: None}.get(direction)
                await self.app.manual_batch(dir_enum, high_speed, car_ids)

        # 非阻塞 stdin：select.select + os.read（不用线程泄漏，不用 ibuf）
        # + deadline 周期性检查（松开方向键立刻停电机）
        import select
        import os

        try:
            tty.setraw(fd)
            render_status()

            try:
                while True:
                    # 1. 检查 deadline（松开方向键检测）
                    if stop_deadline is not None and loop.time() >= stop_deadline:
                        await transition(None, False)
                        stop_deadline = None
                        render_status()
                        continue

                    # 2. select 判断 stdin 是否可读（短超时，每轮都给 deadline 检查机会）
                    r, _, _ = await loop.run_in_executor(
                        None, lambda: select.select([fd], [], [], 0.02)
                    )
                    if not r:
                        continue  # 超时，回顶部再检 deadline

                    # 3. 读一字节（select 保证可读，不阻塞）
                    raw = os.read(fd, 1)
                    if not raw:
                        break

                    # 4. 解析单字节
                    if raw == b'\x1b':
                        # 方向键序列 \e[A 或 \e[1;2A
                        # 非阻塞读后续字节（每个给 10ms）
                        seq = b'\x1b'
                        for _ in range(5):  # 最多 5 个后续字节
                            r2, _, _ = await loop.run_in_executor(
                                None, lambda: select.select([fd], [], [], 0.01)
                            )
                            if not r2:
                                break
                            seq += os.read(fd, 1)
                            if seq[-1] in b'ABCD':
                                break
                        # 方向键固定在 seq 最后字节
                        if len(seq) >= 3 and seq[1:2] == b'[':
                            cmd_char = chr(seq[-1]) if seq[-1] in b'ABCD' else ''
                            is_shift = b';' in seq
                            if cmd_char in ('A', 'C'):
                                await transition('up', is_shift)
                                stop_deadline = loop.time() + MOVE_RELEASE_TIMEOUT
                            elif cmd_char in ('B', 'D'):
                                await transition('down', is_shift)
                                stop_deadline = loop.time() + MOVE_RELEASE_TIMEOUT
                            # 其他方向键忽略
                        # 单独的 ESC = 退出
                        if len(seq) == 1:
                            break
                    elif raw == b' ':
                        # 空格 = 显式立即停 + 清 deadline（用户主动停）
                        stop_deadline = None
                        await transition(None, False)
                        if brake_level > 0:
                            if len(car_ids) == 1:
                                await self.app.manual_brake(brake_level, car_id=car_ids[0])
                            else:
                                await self.app.manual_brake_batch(brake_level, car_ids)
                    elif raw in (b'q', b'Q'):
                        break
                    elif raw == b'\x03':  # Ctrl-C
                        break
                    elif raw in b'01234567':
                        brake_level = raw[0] - ord('0')
                    else:
                        continue

                    render_status()
            finally:
                pass
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            sys.stdout.write('\n')
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            # 退出手动模式：恢复 executor
            # 恢复所有 target executor 的 paused 状态
            for cid in car_ids:
                self.app.executors[cid].paused = exec_was_paused.get(cid, False)

        # 释放刹车 + 停电机 + 切回 auto（不自动 tick，避免 UNKNOWN 状态触发 INITIALIZE）
        if len(car_ids) == 1:
            cid = car_ids[0]
            await self.app.manual_brake(0, car_id=cid)
            await self.app.manual_stop(car_id=cid)
            self.app.manual_mode[cid] = False
        else:
            await self.app.manual_brake_batch(0, car_ids)
            await self.app.manual_batch(None, False, car_ids)
        # 只有已初始化的电梯才恢复自动调度（UNKNOWN 状态停在原地不动）
        if self.app.car.state == CarState.READY:
            await self.app._tick()
        print('[manual] 已退出手动控制')

    async def _do_init(self, args: list[str]) -> None:
        """
        用法:
          /init [<up|down> [<floor>]]
          /car 1 init <up|down> <floor>

        程序刚启动时 IO 缓存为空（没收到 bitmap），get_input 读到所有信号都是 0，
        导致 init 误判"没在限位上"往上跑撞 2 限位。
        必须在第一次手动操作（如按轿内按钮）收到 IO2HTTP bitmap 后才能初始化。
        """
        if not self.app.io._input_cache:
            print('[init] 错误：尚未收到 PLC IO 状态（bitmap 为空）。')
            print('       请先操作一个按钮（如轿内选层按钮），')
            print('       触发 IO2HTTP 推送完整 I 区 bitmap 后再重试。')
            return
        direction = None
        target_floor = None
        if args:
            if args[0] in ('up', 'down'):
                direction = args[0]
                if len(args) > 1:
                    try:
                        target_floor = int(args[1])
                    except ValueError:
                        print(f'楼层必须是整数: {args[1]}')
                        return
            else:
                print('参数错误: 第一个参数必须是 up 或 down')
                print('用法: /car <id> init <up|down> [<floor>]')
                return
        await self.app.reset(direction=direction, target_floor=target_floor,
                             car_id=self.current_car_id)
        dir_str = direction or self.app.executor.init_direction
        floor_str = str(target_floor) if target_floor else '1（默认）'
        print(f'car {self.current_car_id} 初始化: {dir_str} 目标楼层={floor_str}')

    async def _do_init_batch(self, car_ids: list[int],
                             sub_args: list[str]) -> None:
        """批量 init：/car 1,2,3,4,5,6 init <dir> <floorlist>"""
        # 解析方向列表
        dirs: list[str | None] = []
        if sub_args and sub_args[0] in ('up', 'down'):
            dirs = [sub_args[0]]  # 广播
            target_token = sub_args[1] if len(sub_args) > 1 else None
        elif sub_args:
            try:
                dirs = self._parse_dir_list(sub_args[0])
            except Exception:
                print(f'方向参数无效: {sub_args[0]}')
                return
            target_token = sub_args[1] if len(sub_args) > 1 else None
        else:
            target_token = None

        # 解析楼层列表
        floors: list[int] = []
        if target_token:
            try:
                floors = [int(x.strip()) for x in target_token.split(',') if x.strip()]
            except ValueError:
                print(f'楼层列表无效: {target_token}')
                return

        N = len(car_ids)
        # 验证方向
        if len(dirs) not in (0, 1, N):
            print(f'方向数量 ({len(dirs)}) 与轿厢数量 ({N}) 不匹配')
            print(f'  用法: /car {",".join(map(str,car_ids))} init <dir> <floor1,floor2,...>')
            return
        if len(dirs) == 1:
            dirs = dirs * N
        elif len(dirs) == 0:
            dirs = [None] * N

        # 验证楼层：没有则每部车默认 1 楼；1 个则广播到所有车
        if not floors:
            floors = [1] * N
        elif len(floors) == 1:
            floors = floors * N
        if len(floors) != N:
            print(f'楼层数量 ({len(floors)}) 与轿厢数量 ({N}) 不匹配')
            print(f'  用法: /car {",".join(map(str,car_ids))} init <dir> <floor1,floor2,...>')
            return

        # 执行
        parts: list[str] = []
        for cid, d, f in zip(car_ids, dirs, floors):
            await self.app.reset(direction=d, target_floor=f, car_id=cid)
            dir_label = d or self.app.executor.init_direction
            parts.append(f'car{cid} {dir_label}→{f}')
        print(f'[batch init] {", ".join(parts)}')

    async def _do_call_batch(self, car_ids: list[int],
                             sub_args: list[str]) -> None:
        """批量 call：/car all call 1,4,7,2,5,8"""
        if not sub_args:
            print(f'缺少楼层列表')
            print(f'  用法: /car {",".join(map(str,car_ids))} call <floor1,floor2,...>')
            return
        try:
            floors = [int(x.strip()) for x in sub_args[0].split(',') if x.strip()]
        except ValueError:
            print(f'楼层列表无效: {sub_args[0]}')
            return

        N = len(car_ids)
        if len(floors) == 1:
            floors = floors * N
        elif len(floors) != N:
            print(f'楼层数量 ({len(floors)}) 与轿厢数量 ({N}) 不匹配')
            return

        parts: list[str] = []
        for cid, f in zip(car_ids, floors):
            if self.app.manual_mode.get(cid, False):
                await self.app.manual_auto(car_id=cid)
            await self.app.call_internal(f, car_id=cid)
            parts.append(f'car{cid}→L{f}')
        print(f'[batch call] {", ".join(parts)}')

    async def _do_call(self, args: list[str]) -> None:
        if not args:
            print('用法: /call <floor>')
            return
        try:
            floor = int(args[0])
        except ValueError:
            print(f'楼层必须是整数: {args[0]}')
            return
        # 手动模式下自动切回 auto 再发内召
        if self.app.manual_mode.get(self.current_car_id, False):
            await self.app.manual_auto(car_id=self.current_car_id)
        await self.app.call_internal(floor, car_id=self.current_car_id)
        print(f'car {self.current_car_id} 已内召 L{floor}')

    async def cmd_clear(self, args: list[str]) -> None:
        await self.app.clear_outputs()

    async def cmd_debug(self, args: list[str]) -> None:
        if not args or args[0] != 'show':
            print('用法: /debug show <pass_floor|input_change>')
            return
        if len(args) < 2:
            # 显示当前所有监视项状态
            pf = '启用' if self.pass_floor_monitor_enabled else '禁用'
            ic = '启用' if self.input_change_monitor_enabled else '禁用'
            ws = '启用' if self.ws_monitor_enabled else '禁用'
            et = '启用' if self.exec_trace_enabled else '禁用'
            es = '启用' if self.elevator_speed_enabled else '禁用'
            print(f'pass_floor 监视:             {pf}')
            print(f'input_change 监视:           {ic}')
            print(f'websocket_connect_status 监视: {ws}')
            print(f'exec_trace 监视:             {et}')
            print(f'elevator_speed 监视:         {es}')
            return
        topic = args[1]
        if topic == 'pass_floor':
            self._toggle_pass_floor_monitor()
        elif topic == 'input_change':
            self._toggle_input_change_monitor()
        elif topic == 'websocket_connect_status':
            self._toggle_ws_monitor()
        elif topic == 'exec_trace':
            self._toggle_exec_trace()
        elif topic == 'elevator_speed':
            self._toggle_elevator_speed()
        else:
            print(f'未知 show 主题: {topic}')

    def _toggle_pass_floor_monitor(self) -> None:
        """toggle pass_floor 监视：启用 / 禁用"""
        if self.pass_floor_monitor_enabled:
            self._disable_pass_floor_monitor()
            print('[debug] pass_floor 监视已禁用')
        else:
            self._enable_pass_floor_monitor()
            print('[debug] pass_floor 监视已启用')

    def _enable_pass_floor_monitor(self) -> None:
        self.pass_floor_monitor_enabled = True
        for cid in CAR_IDS:
            try:
                up_addr = self.app.mapper.db_to_i(
                    self.app.mapper.addr_input('level_up', cid)
                )
                down_addr = self.app.mapper.db_to_i(
                    self.app.mapper.addr_input('level_down', cid)
                )
            except KeyError:
                continue
            perfect = (self.app.io.get_input(up_addr) == 1
                       and self.app.io.get_input(down_addr) == 1)
            self._pass_floor_last_perfect[cid] = perfect
        # 存住 bound method 引用，禁用时按同一引用移除（每次访问 self._on_pass_floor_event
        # 会产生新的 bound method 对象，按 id/== 比对会失败）
        self._pass_floor_listener_ref = self._on_pass_floor_event
        self.app.io.add_listener(self._pass_floor_listener_ref)

    def _disable_pass_floor_monitor(self) -> None:
        self.pass_floor_monitor_enabled = False
        ref = getattr(self, '_pass_floor_listener_ref', None)
        if ref is not None:
            self.app.io.remove_listener(ref)
            self._pass_floor_listener_ref = None

    def _check_perfect_leveling(self) -> bool:
        """仅保留签名兼容性（实际未使用，多车检测在 _on_pass_floor_event 内做）"""
        return False

    async def _on_pass_floor_event(self, event: IOEvent) -> None:
        """IO listener：扫描所有车的 level_up & level_down，detect 完美平层上升沿"""
        for cid in CAR_IDS:
            try:
                up_addr = self.app.mapper.db_to_i(
                    self.app.mapper.addr_input('level_up', cid)
                )
                down_addr = self.app.mapper.db_to_i(
                    self.app.mapper.addr_input('level_down', cid)
                )
            except KeyError:
                continue
            up_now = self.app.io.get_input(up_addr)
            down_now = self.app.io.get_input(down_addr)
            perfect_now = (up_now == 1 and down_now == 1)
            if perfect_now and not self._pass_floor_last_perfect.get(cid, False):
                pos = self.app.cars[cid].position
                pos_str = f'L{pos}' if pos is not None else 'N/A'
                print(f'[DEBUG] car{cid} pass_floor {pos_str}', file=sys.stderr)
                sys.stderr.flush()
            self._pass_floor_last_perfect[cid] = perfect_now

    def _toggle_input_change_monitor(self) -> None:
        if self.input_change_monitor_enabled:
            self._disable_input_change_monitor()
            print('[debug] input_change 监视已禁用')
        else:
            self._enable_input_change_monitor()
            print('[debug] input_change 监视已启用')

    def _enable_input_change_monitor(self) -> None:
        self.input_change_monitor_enabled = True
        self._input_change_listener_ref = self._on_input_change_event
        # 保存启用时已收到的 cache 地址，跳过首次 bitmap 同步（从无到有的假变化）
        self._input_change_known = set(self.app.io.get_all_inputs().keys())
        self.app.io.add_listener(self._input_change_listener_ref)

    def _disable_input_change_monitor(self) -> None:
        self.input_change_monitor_enabled = False
        ref = getattr(self, '_input_change_listener_ref', None)
        if ref is not None:
            self.app.io.remove_listener(ref)
            self._input_change_listener_ref = None

    async def _on_input_change_event(self, event: IOEvent) -> None:
        """IO listener：每次任意 I 点变化时打印信号名 + 值"""
        # 跳过首次 bitmap 填入：地址在启用时的 cache 中不存在 → 首次 sync
        if event.i_addr not in self._input_change_known:
            self._input_change_known.add(event.i_addr)
            return
        sig = self.app.mapper.lookup_signal_by_i(event.i_addr)
        if sig is not None:
            car_id, name = sig
            if car_id:
                label = f'car{car_id}.{name}'
            else:
                label = f'hall.{name}'
        else:
            label = '?'
        print(f'[DEBUG] input {event.i_addr} {label} -> {event.bit}', file=sys.stderr)
        sys.stderr.flush()

    def _toggle_ws_monitor(self) -> None:
        if self.ws_monitor_enabled:
            self._disable_ws_monitor()
            print('[debug] websocket 状态监视已禁用')
        else:
            self._enable_ws_monitor()
            print('[debug] websocket 状态监视已启用')

    def _enable_ws_monitor(self) -> None:
        self.ws_monitor_enabled = True
        self._last_ws_connected = self.app.io.ws_connected
        status = '已连接' if self._last_ws_connected else '未连接'
        print(f'[debug] WebSocket: {status}')
        self._ws_monitor_task = asyncio.create_task(self._poll_ws_status())

    def _disable_ws_monitor(self) -> None:
        self.ws_monitor_enabled = False
        if self._ws_monitor_task and not self._ws_monitor_task.done():
            self._ws_monitor_task.cancel()
        self._ws_monitor_task = None

    async def _poll_ws_status(self) -> None:
        """每秒轮询 ws_connected 变化，变化时输出"""
        try:
            while self.ws_monitor_enabled:
                await asyncio.sleep(1.0)
                current = self.app.io.ws_connected
                if current != self._last_ws_connected:
                    self._last_ws_connected = current
                    status = '已连接' if current else '断连'
                    print(f'[DEBUG] websocket {status}', file=sys.stderr)
                    sys.stderr.flush()
        except asyncio.CancelledError:
            pass

    def _toggle_exec_trace(self) -> None:
        """toggle exec_trace：遍历全部 6 部电梯（和 elevator_speed 对齐）

        早期版本只设 self.app.executor（=current_car_id 的 executor），
        切换 /car N 后旧车会被静默禁掉。新行为：所有车同步切换。
        """
        self.exec_trace_enabled = not self.exec_trace_enabled
        for cid in CAR_IDS:
            self.app.executors[cid].exec_log_enabled = self.exec_trace_enabled
        status = '启用' if self.exec_trace_enabled else '禁用'
        print(f'[debug] exec_trace 监视已{status}（全部 {len(CAR_IDS)} 部轿厢）')

    def _toggle_elevator_speed(self) -> None:
        if self.elevator_speed_enabled:
            self._disable_elevator_speed()
        else:
            self._enable_elevator_speed()

    def _enable_elevator_speed(self) -> None:
        self.elevator_speed_enabled = True
        speed_map = {'high_speed': '高速', 'decel': '减速', '': '停止'}
        print('[debug] elevator_speed 已启用（监视 6 部电梯档位变化）')
        for cid in CAR_IDS:
            label = speed_map.get(self.app.executors[cid].decel_state, '?')
            print(f'[debug] car{cid} 当前: {label}')
        self._elevator_speed_task = asyncio.create_task(self._poll_elevator_speed())

    def _disable_elevator_speed(self) -> None:
        self.elevator_speed_enabled = False
        if self._elevator_speed_task and not self._elevator_speed_task.done():
            self._elevator_speed_task.cancel()
        self._elevator_speed_task = None
        print('[debug] elevator_speed 监视已禁用')

    async def _poll_elevator_speed(self) -> None:
        speed_map = {'high_speed': '高速', 'decel': '减速', '': '停止'}
        last_states: dict[int, str] = {}
        for cid in CAR_IDS:
            last_states[cid] = self.app.executors[cid].decel_state
        try:
            while self.elevator_speed_enabled:
                await asyncio.sleep(0.2)
                for cid in CAR_IDS:
                    current = self.app.executors[cid].decel_state
                    if current != last_states.get(cid):
                        last_states[cid] = current
                        label = speed_map.get(current, '?')
                        print(f'[DEBUG] car{cid} speed {label}', file=sys.stderr)
                        sys.stderr.flush()
        except asyncio.CancelledError:
            pass

    async def cmd_reload(self, args: list[str]) -> None:
        await self.app.reload()
        print('已重载 config / io_config / display_config')

    async def cmd_quit(self, args: list[str]) -> None:
        # 在 run() 里直接 break
        pass