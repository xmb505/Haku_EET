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

from .actions import ActionKind
from .app import App


HELP_TEXT = """
可用命令:
  /help                          显示这个帮助
  /status [car <id>]             查看玩家状态（默认 car 1）
  /cars                          列出已启用的轿厢 ID
  /car <id> <action> [args...]   指定轿厢执行命令
    动作: init / call / status / manual / auto
  /init [down|up]                手动触发初始化
  /call <floor>                  内召：到目标楼层
  /clear                         将所有输出位置零（不含 ready 信号）
  /sim input <signal> <0|1|toggle>  模拟输入变化
  /sim position <floor>          直接修改轿厢位置
  /display <floor|up|dn|fault|A> 设置 7 段显示
  /actions                       查看动作队列长度
  /algo list | show | set <name> 算法管理
  /debug on|off                  调试日志开关
  /reload                        重载全部 config
  /quit                          退出

示例:
  /car 1 init                    1 号梯初始化（完整流程：全速→触 1 限位→减速→完美平层）
  /car 1 manual                  进入手动控制（方向键控制，ESC 退出）
  /car 1 auto                    切回自动控制
  /car 1 call 5                  1 号梯内召 5 楼
  /call 3                        默认 1 号梯内召 3 楼
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
            'help': self.cmd_help,
            'status': self.cmd_status,
            'cars': self.cmd_cars,
            'car': self.cmd_car,
            'init': self.cmd_init,
            'call': self.cmd_call,
            'sim': self.cmd_sim,
            'display': self.cmd_display,
            'actions': self.cmd_actions,
            'algo': self.cmd_algo,
            'debug': self.cmd_debug,
            'reload': self.cmd_reload,
            'clear': self.cmd_clear,
            'quit': self.cmd_quit,
        }

    def _resolve_car_id(self, args: list[str]) -> int:
        """从参数里提取 car_id（如果有），否则用当前选中的"""
        if args and args[0].isdigit():
            return int(args[0])
        return self.current_car_id

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
            # 有子命令的一级命令（输入完整名 + Tab 自动补空格进二级）
            commands_with_subs = {
                '/car': ['init', 'call', 'status', 'manual', 'auto'],
            }

            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith('/'):
                    return

                # 找到光标前最近一个空格，把输入分成"前缀"+"当前单词"
                last_space = text.rfind(' ')
                prefix_text = text[:last_space + 1] if last_space >= 0 else ''
                current_word = text[last_space + 1:] if last_space >= 0 else text

                # 一级命令补全：还没输空格
                if not prefix_text.strip():
                    matches = [c for c in self.cmds if c.startswith(current_word)]
                    for m in matches:
                        # 如果用户已经输完整的一级命令，且这个命令有子命令，
                        # 自动补空格进二级（如 /car + Tab → /car _）
                        is_exact_sub = current_word == m and m in self.commands_with_subs
                        append = ' ' if is_exact_sub else ''
                        yield Completion(m + append, start_position=-len(current_word))
                    return

                # 二级子命令补全（/car 后）
                parts = prefix_text.split()
                if not parts:
                    return
                cmd = parts[0]
                if cmd in self.commands_with_subs:
                    subs = self.commands_with_subs[cmd]
                    if current_word == '':
                        # 刚输入完空格，列出所有子命令
                        for s in subs:
                            yield Completion(s, start_position=0)
                    else:
                        for s in subs:
                            if s.startswith(current_word):
                                yield Completion(s, start_position=-len(current_word))

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

    async def cmd_status(self, args: list[str]) -> None:
        snap = self.app.status_snapshot()
        car = snap['car']
        # 检查参数是否指定了 car_id
        requested = self._resolve_car_id(args)
        if requested != self.app.car.car_id:
            print(f'轿厢 {requested} 未启用（当前只实例化了 car {self.app.car.car_id}）')
            return
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
        print(f'  - car {self.app.car.car_id}')

    async def cmd_car(self, args: list[str]) -> None:
        """
        /car <id> <action> [args...]  切换或路由命令到指定 car
        /car <id>                     切换当前选中的 car（影响后续命令默认值）
        """
        if not args:
            print('用法: /car <id> [init|call|status] [...]')
            return
        try:
            car_id = int(args[0])
        except ValueError:
            print(f'car_id 必须是整数: {args[0]}')
            return
        sub_action = args[1] if len(args) > 1 else None
        sub_args = args[2:]

        if car_id != self.app.car.car_id:
            print(f'轿厢 {car_id} 未启用（当前只实例化了 car {self.app.car.car_id}）')
            print(f'提示: 修改 config.yaml 的 elevator.car_id 可启用其他轿厢')
            return

        # 切换当前选中的 car
        self.current_car_id = car_id

        if sub_action is None:
            print(f'已切换当前轿厢: car {car_id}')
            return

        # 路由到对应命令
        if sub_action == 'init':
            await self.cmd_init(sub_args)
        elif sub_action == 'call':
            await self.cmd_call(sub_args)
        elif sub_action == 'status':
            await self.cmd_status(sub_args)
        elif sub_action == 'manual':
            await self._run_manual(car_id)
        elif sub_action == 'auto':
            await self.app.manual_auto()
        elif sub_action == 'goto':
            await self.cmd_call(sub_args)
        else:
            print(f'未知子命令: {sub_action}')

    async def _run_manual(self, car_id: int) -> None:
        """
        手动控制 raw key loop（前后端彻底解耦）：

        输入 → 高层动作（manual_up/down/stop/brake）
        输出 → 单行 \\r 覆盖的状态栏（永不 print 干扰）
        位置 → 后台任务每秒推 1 层（仅 simulate 模式有效）

        操作：
            ↑/↓/←/→    上/下行（低速）
            Shift+↑/↓  上/下行（高速）
            空格      立即停 + 刹车
            数字 1-7  设置刹车档位
            0         释放刹车
            ESC/q/Ctrl-C  退出

        状态聚合（重复按方向键 → 幂等，不重写 IO）：
            current_motion = ('up', True/False) / ('down', ...) / None
            direction + speed 相同时跳过手动_up/down 调用
        """
        if not sys.stdin.isatty():
            print('[manual] 当前 stdin 不是 tty，无法捕获方向键。请在真实终端运行。')
            return

        from core.player import Direction, CarState

        print()
        print('=' * 50)
        print(f'  car {car_id} 手动控制模式')
        print('  ↑ ↓ / ← →   = 上下行（低速）')
        print('  Shift+↑↓    = 上下行（高速）')
        print('  空格         = 立即停 + 刹车')
        print('  数字键 1-7   = 设置刹车档位（0=释放, 7=全刹）')
        print('  ESC / q      = 退出手动控制')
        print('=' * 50)

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        loop = asyncio.get_running_loop()
        brake_level = 0
        # 松开方向键自动停的超时：50ms 没新输入就停电机
        # 典型终端 key repeat 间隔 20-40ms，所以按住时永不停
        # 松手 → 50ms 内 deadline 到期 → 立即停电机
        MOVE_TIMEOUT = 0.05
        stop_deadline: float | None = None
        current_motion: tuple[str, bool] | None = None

        # 没位置的话给个初值（simulate 用），让位置模拟器能跑
        if self.app.car.position is None:
            self.app.car.position = 1

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
                f'\r[car {car_id}] L={pos}{speed_label} 方向={dir_label} 门={door} '
                f'刹车={brake_level} {fault_str}      '
            )
            sys.stdout.write(line)
            sys.stdout.flush()

        # 后台任务：simulate 模式下每秒推 1 层位置（让状态实时变）
        sim_stop = asyncio.Event()

        async def position_simulator():
            while not sim_stop.is_set():
                try:
                    await asyncio.wait_for(sim_stop.wait(), timeout=0.5)
                    break
                except asyncio.TimeoutError:
                    pass
                car = self.app.car
                if not self.app.manual_mode:
                    break
                if car.direction == Direction.UP and car.position is not None:
                    if car.position >= 10:
                        await self.app.manual_stop()
                        current_motion = None
                    else:
                        car.position += 1
                    render_status()
                elif car.direction == Direction.DOWN and car.position is not None:
                    if car.position <= 1:
                        await self.app.manual_stop()
                        current_motion = None
                    else:
                        car.position -= 1
                    render_status()

        async def transition(direction: str | None, high_speed: bool):
            """按当前运动状态路由。direction=None = 停。"""
            nonlocal current_motion
            target = None if direction is None else (direction, high_speed)
            if current_motion == target:
                return  # 幂等
            current_motion = target
            if direction == 'up':
                await self.app.manual_up(high_speed=high_speed)
            elif direction == 'down':
                await self.app.manual_down(high_speed=high_speed)
            elif direction is None:
                await self.app.manual_stop()

        # 最简单的非阻塞 stdin：select.select + os.read（不用线程泄漏，不用 ibuf）
        import select
        import os

        try:
            tty.setraw(fd)
            render_status()

            sim_task = asyncio.create_task(position_simulator())

            try:
                while True:
                    # 1. 检查 deadline（松开方向键检测）
                    if stop_deadline is not None and loop.time() >= stop_deadline:
                        await transition(None, False)
                        stop_deadline = None
                        render_status()
                        continue

                    # 2. select 判断 stdin 是否可读（带 timeout）
                    r, _, _ = await loop.run_in_executor(
                        None, lambda: select.select([fd], [], [], MOVE_TIMEOUT)
                    )
                    if not r:
                        continue  # 超时，回顶部检查 deadline

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
                                stop_deadline = loop.time() + MOVE_TIMEOUT
                            elif cmd_char in ('B', 'D'):
                                await transition('down', is_shift)
                                stop_deadline = loop.time() + MOVE_TIMEOUT
                            # 其他方向键忽略
                        # 单独的 ESC = 退出
                        if len(seq) == 1:
                            break
                    elif raw == b' ':
                        stop_deadline = None
                        await transition(None, False)
                        if brake_level > 0:
                            await self.app.manual_brake(brake_level)
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
                sim_stop.set()
                sim_task.cancel()
                try:
                    await sim_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            sys.stdout.write('\n')
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

        # 释放刹车 + 停电机 + 切回 auto（不自动 tick，避免 UNKNOWN 状态触发 INITIALIZE）
        await self.app.manual_brake(0)
        await self.app.manual_stop()
        self.app.manual_mode = False
        # 只有已初始化的电梯才恢复自动调度（UNKNOWN 状态停在原地不动）
        if self.app.car.state == CarState.READY:
            await self.app._tick()
        print('[manual] 已退出手动控制')

    async def cmd_init(self, args: list[str]) -> None:
        direction = args[0] if args else None
        await self.app.reset(direction=direction)
        print(f'car {self.app.car.car_id} 初始化已触发，方向={direction or self.app.executor.init_direction}')

    async def cmd_call(self, args: list[str]) -> None:
        if not args:
            print('用法: /call <floor>')
            return
        try:
            floor = int(args[0])
        except ValueError:
            print(f'楼层必须是整数: {args[0]}')
            return
        # 手动模式下自动切回 auto 再发内召
        if self.app.manual_mode:
            await self.app.manual_auto()
        await self.app.call_internal(floor)
        print(f'car {self.app.car.car_id} 已内召 L{floor}')

    async def cmd_clear(self, args: list[str]) -> None:
        await self.app.clear_outputs()

    async def cmd_sim(self, args: list[str]) -> None:
        if not self.app.simulate:
            print('错误: /sim 只在 --simulate 模式下可用')
            return
        if len(args) < 2:
            print('用法:')
            print('  /sim input <signal> <0|1|toggle>   模拟输入')
            print('  /sim position <floor>              直接改位置')
            return
        sub = args[0]
        if sub == 'input':
            sig = args[1]
            val_arg = args[2] if len(args) > 2 else '1'
            if val_arg == 'toggle':
                # 取当前 I 地址的缓存值
                try:
                    db = self.app.mapper.addr_input(sig, self.app.car.car_id)
                except KeyError:
                    print(f'未知输入信号: {sig}')
                    return
                i_addr = self.app.mapper.db_to_i(db)
                cur = self.app.io.get_input(i_addr)
                val = 0 if cur else 1
            else:
                val = 1 if val_arg == '1' else 0
            try:
                db = self.app.mapper.addr_input(sig, self.app.car.car_id)
            except KeyError:
                print(f'未知输入信号: {sig}（car_id={self.app.car.car_id}）')
                return
            i_addr = self.app.mapper.db_to_i(db)
            self.app.io.simulate_input(i_addr, val)
            print(f'已模拟 {sig} ({i_addr}) = {val}')
        elif sub == 'position':
            try:
                floor = int(args[1])
            except ValueError:
                print('位置必须是整数')
                return
            self.app.car.position = floor
            self.app.car.state = self.app.car.state  # 保持
            await self.app._tick()
            print(f'已将位置改为 L{floor}')
        else:
            print(f'未知子命令: {sub}')

    async def cmd_display(self, args: list[str]) -> None:
        if not args:
            print('用法: /display <floor|up|dn|fault|blank>')
            return
        target = args[0]
        # 字符别名映射
        glyph_map = {
            'up': 'up',
            'dn': 'down',
            'down': 'down',
            'fault': 'fault',
            'blank': 'blank',
        }
        if target in glyph_map:
            from .actions import Action
            await self.app.action_queue.put(
                Action(ActionKind.SET_DISPLAY, glyph=glyph_map[target])
            )
            print(f'已设置 7 段显示为字符 {glyph_map[target]!r}')
            return
        # 楼层号
        try:
            floor = int(target)
        except ValueError:
            print(f'参数必须是楼层号或字符别名 (up/dn/fault/blank): {target}')
            return
        await self.app.set_display(floor)
        print(f'已设置 7 段显示为 L{floor}')

    async def cmd_actions(self, args: list[str]) -> None:
        print(f'动作队列长度: {self.app.action_queue.qsize()}')

    async def cmd_algo(self, args: list[str]) -> None:
        if not args:
            print('用法: /algo list | show | set <name>')
            return
        sub = args[0]
        if sub == 'list':
            print('可用算法:')
            for name in self.app.available_algorithms():
                marker = ' ← 当前' if name == self.app.algorithm.name else ''
                print(f'  - {name}{marker}')
        elif sub == 'show':
            print(f'当前算法: {self.app.algorithm.name}')
        elif sub == 'set':
            if len(args) < 2:
                print('用法: /algo set <name>')
                return
            try:
                await self.app.set_algorithm(args[1])
            except KeyError as e:
                print(f'错误: {e}')
                return
            print(f'已切换到算法: {args[1]}')
        else:
            print(f'未知子命令: {sub}')

    async def cmd_debug(self, args: list[str]) -> None:
        if not args:
            print(f'debug = {self.app.debug}')
            return
        self.app.debug = args[0] == 'on'
        print(f'debug = {self.app.debug}')

    async def cmd_reload(self, args: list[str]) -> None:
        await self.app.reload()
        print('已重载 config / io_config / display_config')

    async def cmd_quit(self, args: list[str]) -> None:
        # 在 run() 里直接 break
        pass