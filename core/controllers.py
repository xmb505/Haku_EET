"""
controllers.py —— 电机 / 门控制器

封装 IO 写操作，上层（executor）只调用 move_to / open_door 等高层方法，
不接触信号名和 IO 地址。

PLC 刹车接法（代码假设,验证现场硬件后确认）:
    电磁刹车型 —— 通电刹死 / 失电释放。
    所以 brake_X = 0 = 释放(默认常态,弹簧推开)
       brake_X = 1 = 刹死(线圈通电 → 电磁力刹住)

    整套代码以下面这个语义为准:
      set_brakes(0, 0, 0) = 释放刹车(让电机能驱动)
      set_brakes(1, 1, 1) = 全刹(7 档 max)
    如果现场 PLC 接法相反,需要反转 set_brakes 里的 0/1 映射。
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from .io_client import IOClient, IOEvent
    from .io_mapper import IOMapper


class MotorController:
    """motor / contactor / brake controller"""

    def __init__(self, io: IOClient, mapper: IOMapper, car_id: int,
                 io_write: IOClient | None = None) -> None:
        self.io = io
        self.io_write = io_write if io_write is not None else io
        self.mapper = mapper
        self.car_id = car_id

    async def start(self, high_speed: bool = True,
                    direction: str | None = None) -> None:
        up = 0
        down = 0
        if direction == 'up':
            up, down = 1, 0
        elif direction == 'down':
            up, down = 0, 1
        elif direction is None:
            up = down = 0
        await self.io_write.set_many({
            self.mapper.addr_output('up_contactor', self.car_id): up,
            self.mapper.addr_output('down_contactor', self.car_id): down,
            self.mapper.addr_output('high_speed_contactor', self.car_id): 1 if high_speed else 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0 if high_speed else 1,
            self.mapper.addr_output('motor_start', self.car_id): 1,
        })

    async def stop(self) -> None:
        await self.io_write.set_many({
            self.mapper.addr_output('up_contactor', self.car_id): 0,
            self.mapper.addr_output('down_contactor', self.car_id): 0,
            self.mapper.addr_output('high_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('motor_start', self.car_id): 0,
        })

    async def release_brakes(self) -> None:
        await self.set_brakes(0, 0, 0)

    async def hold_stop(self) -> None:
        await self.io_write.set_many({
            self.mapper.addr_output('up_contactor', self.car_id): 0,
            self.mapper.addr_output('down_contactor', self.car_id): 0,
            self.mapper.addr_output('high_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('motor_start', self.car_id): 0,
            self.mapper.addr_output('brake_1', self.car_id): 1,
            self.mapper.addr_output('brake_2', self.car_id): 1,
            self.mapper.addr_output('brake_3', self.car_id): 1,
        })

    async def set_direction_indicator(self, direction: str | None) -> None:
        up = 1 if direction == 'up' else 0
        down = 1 if direction == 'down' else 0
        await self.io_write.set_many({
            self.mapper.addr_output('up_indicator', self.car_id): up,
            self.mapper.addr_output('down_indicator', self.car_id): down,
        })

    async def set_speed(self, high_speed: bool) -> None:
        await self.io_write.set_many({
            self.mapper.addr_output('high_speed_contactor', self.car_id): 1 if high_speed else 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0 if high_speed else 1,
            self.mapper.addr_output('motor_start', self.car_id): 1,
        })

    async def set_brakes(self, b1: int = 0, b2: int = 0, b3: int = 0) -> None:
        await self.io_write.set_many({
            self.mapper.addr_output('brake_1', self.car_id): b1,
            self.mapper.addr_output('brake_2', self.car_id): b2,
            self.mapper.addr_output('brake_3', self.car_id): b3,
        })

    async def set_brake_level(self, level: int) -> None:
        b1 = 1 if (level & 0b001) else 0
        b2 = 1 if (level & 0b010) else 0
        b3 = 1 if (level & 0b100) else 0
        await self.set_brakes(b1, b2, b3)

    async def all_off(self) -> None:
        await self.io_write.set_many({
            self.mapper.addr_output('up_contactor', self.car_id): 0,
            self.mapper.addr_output('down_contactor', self.car_id): 0,
            self.mapper.addr_output('high_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('motor_start', self.car_id): 0,
            self.mapper.addr_output('brake_1', self.car_id): 0,
            self.mapper.addr_output('brake_2', self.car_id): 0,
            self.mapper.addr_output('brake_3', self.car_id): 0,
        })


class DoorController:
    """door relay controller with self-managed IO listeners

    Manages its own sensor listening during open/close cycles:
      - open:  listens for door_open_done (completion) + floor_door_lock (wrong-floor check)
      - close: listens for door_close_done (completion) + light_curtain (breach reversal)

    Callers push open()/close() then await wait_done() for the result:
      'done'        — normal completion
      'breach'      — light curtain triggered during close; door reversed to open
      'wrong_floor' — floor door lock mismatch on open

    on_breach callback fires when light_curtain interrupts closing.
    on_light_curtain callback fires on any light_curtain=true during open/close cycles.
    """

    def __init__(self, io: IOClient, mapper: IOMapper, car_id: int,
                 io_write: IOClient | None = None,
                 on_breach: Callable[[], Awaitable[None]] | None = None,
                 on_light_curtain: Callable[[], Awaitable[None]] | None = None) -> None:
        self.io = io
        self.io_write = io_write if io_write is not None else io
        self.mapper = mapper
        self.car_id = car_id
        self._on_breach = on_breach
        self._on_light_curtain = on_light_curtain

        self._done = asyncio.Event()
        self._result: str = 'done'
        self._listeners: list[Callable] = []
        self._car_pos: int | None = None

    # ---- public API ----

    async def open(self) -> None:
        """initiate door open; caller awaits wait_done() for result"""
        self._done.clear()
        self._result = 'done'
        self._remove_listeners()
        self._listeners.append(self.io.add_listener(self._on_open_event))
        await self.io_write.set_many({
            self.mapper.addr_output('door_open_relay', self.car_id): 1,
            self.mapper.addr_output('door_close_relay', self.car_id): 0,
        })

    async def close(self) -> None:
        """initiate door close; caller awaits wait_done() for result"""
        self._done.clear()
        self._result = 'done'
        self._remove_listeners()
        self._listeners.append(self.io.add_listener(self._on_close_event))
        await self.io_write.set_many({
            self.mapper.addr_output('door_open_relay', self.car_id): 0,
            self.mapper.addr_output('door_close_relay', self.car_id): 1,
        })

    async def wait_done(self) -> str:
        """await door action completion; returns 'done' | 'breach' | 'wrong_floor'"""
        await self._done.wait()
        return self._result

    def cancel(self) -> None:
        """force-complete current door action (for emergency stop)"""
        self._remove_listeners()
        self._done.set()

    async def all_off(self) -> None:
        """clear all door relays"""
        self._remove_listeners()
        await self.io_write.set_many({
            self.mapper.addr_output('door_open_relay', self.car_id): 0,
            self.mapper.addr_output('door_close_relay', self.car_id): 0,
        })

    # ---- internal IO event handlers ----

    async def _on_open_event(self, event: IOEvent) -> None:
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None or sig[0] != self.car_id:
            return
        name = sig[1]

        if name == 'door_open_done' and event.bit == 1:
            self._remove_listeners()
            self._done.set()

        elif name == 'light_curtain' and event.bit == 1:
            if self._on_light_curtain is not None:
                await self._on_light_curtain()

        elif name.startswith('floor_door_lock_') and event.bit == 0:
            # floor lock released — check if it matches car position
            try:
                lock_floor = int(name[len('floor_door_lock_'):])
            except ValueError:
                return
            car_pos = self._car_pos
            if car_pos is not None and lock_floor != car_pos:
                self._result = 'wrong_floor'
                self._remove_listeners()
                self._done.set()

    async def _on_close_event(self, event: IOEvent) -> None:
        sig = self.mapper.lookup_signal_by_i(event.i_addr)
        if sig is None or sig[0] != self.car_id:
            return
        name = sig[1]

        if name == 'door_close_done' and event.bit == 1:
            self._remove_listeners()
            self._done.set()

        elif name == 'light_curtain' and event.bit == 1:
            print(f'[door] car{self.car_id} breach, reversing to open')
            # breach: reverse to open
            self._remove_listeners()
            self._result = 'breach'
            # cut close relay, engage open relay
            await self.io_write.set_many({
                self.mapper.addr_output('door_open_relay', self.car_id): 1,
                self.mapper.addr_output('door_close_relay', self.car_id): 0,
            })
            # broadcast breach event to upper layers
            if self._on_breach is not None:
                await self._on_breach()
            # register open listener and wait for door_open_done
            self._listeners.append(self.io.add_listener(self._on_open_event))

    # ---- internal helpers ----

    def _remove_listeners(self) -> None:
        for fn in self._listeners:
            self.io.remove_listener(fn)
        self._listeners.clear()

    def set_car_position(self, pos: int | None) -> None:
        """set car position for wrong-floor check during open"""
        self._car_pos = pos
