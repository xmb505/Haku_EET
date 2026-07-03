"""
io_client.py —— 异步 IO2HTTP 客户端

职责:
    - HTTP POST /gpio 写输出
    - WebSocket 订阅 gpio_change 事件
    - 维护输入电平缓存
    - 提供 add_listener() 给上层订阅 IO 变化

支持 simulate=True 模式，跳过真实网络（无硬件调试用）。
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp
import websockets


@dataclass(frozen=True)
class IOEvent:
    """一个输入电平变化事件"""
    i_addr: str      # 例如 'I4.2'
    bit: int         # 0 或 1


Listener = Callable[[IOEvent], Awaitable[None]]


class IOClient:
    def __init__(
        self,
        http_url: str = 'http://192.168.1.201:8080/gpio',
        ws_url: str = 'ws://192.168.1.201:8081/',
        alias: str = 'plc',
        simulate: bool = False,
        debug: bool = False,
        reconnect_delay: float = 3.0,
    ) -> None:
        self.http_url = http_url
        self.ws_url = ws_url
        self.alias = alias
        self.simulate = simulate
        self.debug = debug
        self.reconnect_delay = reconnect_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._listeners: list[Listener] = []
        self._input_cache: dict[str, int] = {}     # i_addr → bit
        self._output_cache: dict[str, int] = {}    # db_addr → value
        self._running = False

    # ===== 生命周期 =====

    async def start(self) -> None:
        """启动 WS 订阅循环（simulate 模式跳过）"""
        self._running = True
        if self.simulate:
            if self.debug:
                print('[io] simulate 模式启动，跳过真实网络')
            return
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        """停止并清理"""
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    # ===== 事件订阅 =====

    def add_listener(self, listener: Listener) -> None:
        """注册 IO 变化回调，回调签名: async def listener(event: IOEvent)"""
        self._listeners.append(listener)

    async def _dispatch(self, event: IOEvent) -> None:
        for listener in list(self._listeners):
            try:
                await listener(event)
            except Exception as e:
                print(f'[io] listener error: {e!r}')

    # ===== 写输出 =====

    async def set(self, db_addr: str, value: int) -> None:
        """主动写一个 DB 输出位"""
        bit = 1 if value else 0
        self._output_cache[db_addr] = bit
        if self.simulate:
            if self.debug:
                print(f'[io:sim] SET {db_addr} = {bit}')
            return
        assert self._session is not None
        payload = {
            'alias': self.alias,
            'mode': 'seter',
            'gpio': db_addr,
            'value': bit,
        }
        async with self._session.post(self.http_url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise IOError(f'set {db_addr} 失败: HTTP {resp.status} {body}')

    async def set_many(self, writes: dict[str, int]) -> None:
        """批量写多个 DB 输出位（同一字节的会被 IO2HTTP 自动合并）"""
        # IO2HTTP 的 /gpio 接口每次只接受一条命令，所以这里并发发请求
        # 同字节合并是 IO2HTTP 内部做的（一次 read-modify-write）
        await asyncio.gather(*(self.set(addr, val) for addr, val in writes.items()))

    # ===== 读输入 =====

    def get_input(self, i_addr: str) -> int:
        """读当前输入电平（最近一次 WS 推送或 simulate_input 的值）"""
        return self._input_cache.get(i_addr, 0)

    def get_output(self, db_addr: str) -> int:
        """读最近一次 set 的值（用于调试）"""
        return self._output_cache.get(db_addr, 0)

    def get_all_inputs(self) -> dict[str, int]:
        """返回当前所有 I 区状态的快照"""
        return dict(self._input_cache)

    def _apply_bitmap(self, hex_str: str) -> int:
        """
        解析 IO2HTTP 推送的 bitmap 字段，更新输入缓存

        bitmap 是 hex 字符串，每个字节 8 位，按 I0.0 - I99.7 顺序排列：
            byte 0 bit 0 → I0.0
            byte 0 bit 7 → I0.7
            byte 1 bit 0 → I1.0
            ...
            byte 99 bit 7 → I99.7

        返回成功解析的位数（用于调试）。
        """
        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            if self.debug:
                print(f'[io:ws] bitmap 解析失败: {hex_str[:40]}...')
            return 0

        updated = 0
        for byte_idx, byte_val in enumerate(data):
            for bit_idx in range(8):
                bit = (byte_val >> bit_idx) & 1
                i_addr = f'I{byte_idx}.{bit_idx}'
                if self._input_cache.get(i_addr) != bit:
                    self._input_cache[i_addr] = bit
                    updated += 1
        if self.debug and updated > 0:
            print(f'[io:ws] bitmap 更新 {updated} 位（总 {len(data) * 8} 位）')
        return updated

    # ===== 模拟输入（仅 simulate 模式） =====

    def simulate_input(self, i_addr: str, bit: int) -> None:
        """模拟一个 I 输入变化，触发监听器"""
        if not self.simulate:
            raise RuntimeError('simulate_input 只在 simulate=True 模式下可用')
        bit = 1 if bit else 0
        self._input_cache[i_addr] = bit
        if self.debug:
            print(f'[io:sim] INPUT {i_addr} = {bit}')
        event = IOEvent(i_addr=i_addr, bit=bit)
        # 在事件循环里 dispatch
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._dispatch(event))
        except RuntimeError:
            # 没有事件循环（极少见，兼容用），跳过 dispatch
            # listener 是 async 函数，必须有事件循环才能跑
            pass

    # ===== WebSocket 订阅循环 =====

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    if self.debug:
                        print(f'[io:ws] 已连接 {self.ws_url}')
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        if data.get('type') != 'gpio_change':
                            continue
                        for device in data.get('gpios', []):
                            # 1. 先用 bitmap 同步全局 I 区状态（800 位快照）
                            bitmap_hex = device.get('bitmap')
                            if bitmap_hex:
                                self._apply_bitmap(bitmap_hex)
                            # 2. 再 dispatch 变化位（listener 收到的 bit 与缓存一致）
                            for change in device.get('change_gpio', []):
                                i_addr = change.get('gpio')
                                bit = change.get('bit')
                                if i_addr is None or bit is None:
                                    continue
                                # 确保缓存与 change 一致（bitmap 可能是变化前的快照）
                                self._input_cache[i_addr] = int(bit)
                                await self._dispatch(IOEvent(i_addr=i_addr, bit=int(bit)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.debug:
                    print(f'[io:ws] 错误: {e!r}，{self.reconnect_delay}秒后重连')
                await asyncio.sleep(self.reconnect_delay)