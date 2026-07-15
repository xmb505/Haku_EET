"""
io_client.py —— 异步 IO2HTTP 客户端

职责:
    - HTTP POST /gpio 写输出
    - WebSocket 订阅 gpio_change 事件
    - 维护输入电平缓存
    - 提供 add_listener() 给上层订阅 IO 变化

支持 simulate=True 模式，跳过真实网络（无硬件调试用）。
"""

from __future__ import annotations

import asyncio
import json
import sys
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
        tick_interval_ms: float = 100,
        shared_input_cache: dict | None = None,
    ) -> None:
        """shared_input_cache: 多 IOClient 共享缓存容器(传入 {'input': {}, 'output': {}}
        即跨实例共享输入/输出缓存;传 None 或纯 dict[str, int] 视为仅共享 input_cache)"""
        self.http_url = http_url
        self.ws_url = ws_url
        self.alias = alias
        self.simulate = simulate
        self.debug = debug
        self.reconnect_delay = reconnect_delay
        self._tick_interval = max(0.01, tick_interval_ms / 1000.0)

        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._listeners: list[Listener] = []
        # 输入/输出缓存:可通过 shared_caches 跨实例共享
        # (让"只写"实例也能读到同一 I 区,且 app.io.get_output()
        #  能看到 io_write[cid] 的写入,测试/调试/status_snapshot 正常。)
        if shared_input_cache is not None and isinstance(shared_input_cache, dict):
            # shared_input_cache 实际是一个 dict[str, dict],包含 'input' 和 'output' 子键
            if 'input' in shared_input_cache and 'output' in shared_input_cache:
                self._input_cache = shared_input_cache['input']
                self._output_cache = shared_input_cache['output']
            else:
                # 兼容旧用法:直接当 input_cache 用
                self._input_cache = shared_input_cache
                self._output_cache = {}
        else:
            self._input_cache = {}
            self._output_cache = {}
        self._running = False
        self.ws_connected: bool = False
        # 写合并缓冲区：每部电梯用自己的 buffer + flush,避免 6 部共享拥堵
        self._write_buffer: dict[str, int] = {}
        self._flush_task: asyncio.Task | None = None

    # ===== 生命周期 =====

    async def start(self) -> None:
        """启动 WS 订阅循环 + flush 任务（simulate 模式跳过）

        ws_url=None 表示"只写"模式:不连 WS(bitmap 由别的实例负责),
        但仍起 HTTP session + flush task。
        """
        self._running = True
        if self.simulate:
            if self.debug:
                print('[io] simulate 模式启动，跳过真实网络')
            return
        self._session = aiohttp.ClientSession()
        # 只起 WS 当显式给了 ws_url(否则是"只写"实例,bitmap 由其他实例负责)
        if self.ws_url:
            self._ws_task = asyncio.create_task(self._ws_loop())
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """停止并清理"""
        self._running = False
        # flush remaining writes before shutdown
        if not self.simulate:
            await self._flush_now()
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
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

    def remove_listener(self, listener: Listener) -> None:
        """移除监听器"""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    async def _dispatch(self, event: IOEvent) -> None:
        for listener in list(self._listeners):
            try:
                await listener(event)
            except Exception as e:
                print(f'[io] listener error: {e!r}')

    # ===== 写输出 =====

    async def set(self, db_addr: str, value: int) -> None:
        """写一个 DB 输出位（加入写缓冲区，下一个 tick 批量 flush）"""
        bit = 1 if value else 0
        self._output_cache[db_addr] = bit
        if self.simulate:
            if self.debug:
                print(f'[io:sim] SET {db_addr} = {bit}')
            return
        self._write_buffer[db_addr] = bit

    async def set_many(self, writes: dict[str, int]) -> None:
        """批量写多个 DB 输出位（加入写缓冲区，下一个 tick 批量 flush）"""
        for addr, val in writes.items():
            bit = 1 if val else 0
            self._output_cache[addr] = bit
            if not self.simulate:
                self._write_buffer[addr] = bit

    async def flush_now(self) -> None:
        """强制立即 flush 写缓冲区（不等下个 tick）"""
        if not self.simulate and self._write_buffer:
            await self._flush_now()

    # ===== 写合并定时 flush =====

    async def _flush_loop(self) -> None:
        """每 tick 刷一次写缓冲区"""
        try:
            while self._running:
                await asyncio.sleep(self._tick_interval)
                if self._write_buffer:
                    await self._flush_now()
        except asyncio.CancelledError:
            pass

    async def _flush_now(self) -> None:
        """立即将缓冲区内容通过单次 HTTP POST 批量发送"""
        if not self._write_buffer:
            return
        buf = dict(self._write_buffer)
        self._write_buffer.clear()
        assert self._session is not None
        payload = {
            'alias': self.alias,
            'mode': 'seter',
            'gpios': list(buf.keys()),
            'values': list(buf.values()),
        }
        async with self._session.post(self.http_url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise IOError(f'flush {len(buf)} writes 失败: HTTP {resp.status} {body}')

    def observe_input(self, i_addr: str, bit: int) -> None:
        """观察性更新输入缓存（不影响 IO state，但让 cache 与 event 同步）

        给 executor / 测试用：直接调 on_io_event 时同步 cache，
        模拟"PLC bitmap 推过来时 cache + event 同步更新"的语义。
        """
        self._input_cache[i_addr] = 1 if bit else 0

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

    def set_known_i_addresses(self, addrs: set[str]) -> None:
        """设置已知 I 地址集合（来自 IOMapper）

        _apply_bitmap 派发事件时只针对这些地址，
        避免 800 位全 dispatch 导致 listener 被爆。
        未设置时（默认）所有变化的位都会 dispatch，由 listener 自己过滤。
        """
        self._known_i_addrs: set[str] | None = addrs

    async def _apply_bitmap(self, hex_str: str) -> int:
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
        # 收集变化的位并 dispatch 事件给 listener
        # PLC 的 change_gpio 字段不一定可靠（比如限位被快速越过时
        # 只记录最后一个边沿，导致反向逻辑没启动电梯撞 2 限位）
        # 所以 bitmap 也派发；用 _known_i_addrs 过滤避免 800 位全 dispatch
        known = getattr(self, '_known_i_addrs', None)
        changed_events: list[IOEvent] = []

        if known is not None:
            # 快速路径：只扫描已注册的地址（通常 <50 个 vs 800 位全扫描）
            for i_addr in known:
                # 解析 I{byte_idx}.{bit_idx} → byte_idx, bit_idx
                dot = i_addr.index('.')
                byte_idx = int(i_addr[1:dot])
                bit_idx = int(i_addr[dot + 1:])
                if byte_idx >= len(data):
                    continue
                bit = (data[byte_idx] >> bit_idx) & 1
                if self._input_cache.get(i_addr) != bit:
                    self._input_cache[i_addr] = bit
                    updated += 1
                    changed_events.append(IOEvent(i_addr=i_addr, bit=bit))
        else:
            # 慢路径：全 800 位扫描（首次连接 / 未注册已知地址时）
            for byte_idx, byte_val in enumerate(data):
                for bit_idx in range(8):
                    bit = (byte_val >> bit_idx) & 1
                    i_addr = f'I{byte_idx}.{bit_idx}'
                    if self._input_cache.get(i_addr) != bit:
                        self._input_cache[i_addr] = bit
                        updated += 1
                        changed_events.append(IOEvent(i_addr=i_addr, bit=bit))

        if self.debug and updated > 0:
            print(f'[io:ws] bitmap 更新 {updated} 位（总 {len(data) * 8} 位），'
                  f'dispatch {len(changed_events)} 个事件')
        # 派发变化事件——串行 await，确保 listener 按 bitmap 位顺序处理
        # （1 限位 + 2 限位同时为 1 时，让 executor 先看见 2 限位再急停）
        for ev in changed_events:
            await self._dispatch(ev)
        return updated

    # ===== 模拟输入（仅 simulate 模式） =====

    def simulate_input(self, i_addr: str, bit: int) -> None:
        """模拟一个 I 输入变化，触发监听器"""
        if not self.simulate:
            raise RuntimeError('simulate_input 只在 simulate=True 模式下可用')
        new_val = 1 if bit else 0
        self._input_cache[i_addr] = new_val
        if self.debug:
            print(f'[io:sim] INPUT {i_addr} = {new_val}')
        event = IOEvent(i_addr=i_addr, bit=new_val)
        # 在事件循环里 dispatch
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._dispatch(event))
        except RuntimeError:
            pass

    # ===== WebSocket 订阅循环 =====

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,
                ) as ws:
                    self.ws_connected = True
                    try:
                        async for msg in ws:
                            try:
                                data = json.loads(msg)
                            except json.JSONDecodeError:
                                continue
                            if data.get('type') != 'gpio_change':
                                continue
                            for device in data.get('gpios', []):
                                # 1. bitmap 全帧同步：dispatch 所有变化位
                                bitmap_hex = device.get('bitmap')
                                if bitmap_hex:
                                    await self._apply_bitmap(bitmap_hex)
                                # 2. change_gpio 增量边沿：只 dispatch 真正变化的值
                                for change in device.get('change_gpio', []):
                                    i_addr = change.get('gpio')
                                    bit = change.get('bit')
                                    if i_addr is None or bit is None:
                                        continue
                                    try:
                                        new_val = int(bit)
                                    except (TypeError, ValueError):
                                        continue
                                    if self._input_cache.get(i_addr) != new_val:
                                        self._input_cache[i_addr] = new_val
                                        await self._dispatch(IOEvent(i_addr=i_addr, bit=new_val))
                    finally:
                        self.ws_connected = False
            except asyncio.CancelledError:
                self.ws_connected = False
                raise
            except Exception as e:
                self.ws_connected = False
                if self.debug:
                    print(f'[io:ws] 连接断开: {e!r}，{self.reconnect_delay}s 后重连')
                await asyncio.sleep(self.reconnect_delay)