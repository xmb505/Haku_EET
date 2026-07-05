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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .io_client import IOClient
    from .io_mapper import IOMapper


class MotorController:
    """电机/接触器/刹车控制器"""

    def __init__(self, io: IOClient, mapper: IOMapper, car_id: int) -> None:
        self.io = io
        self.mapper = mapper
        self.car_id = car_id

    async def start(self, high_speed: bool = True,
                    direction: str | None = None) -> None:
        """启动电机：方向接触器 + 速度 + 电机"""
        up = 0
        down = 0
        if direction == 'up':
            up, down = 1, 0
        elif direction == 'down':
            up, down = 0, 1
        elif direction is None:
            up = down = 0
        await self.io.set_many({
            self.mapper.addr_output('up_contactor', self.car_id): up,
            self.mapper.addr_output('down_contactor', self.car_id): down,
            self.mapper.addr_output('high_speed_contactor', self.car_id): 1 if high_speed else 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0 if high_speed else 1,
            self.mapper.addr_output('motor_start', self.car_id): 1,
        })

    async def stop(self) -> None:
        """停电机 + 清接触器（不动刹车状态）

        刹车状态由调用方显式管理：
            - 自动模式：start 前 release_brakes，stop 后保持
            - 手动模式：用户设档位 set_brake_level，退出时 release_brakes
        """
        await self.io.set_many({
            self.mapper.addr_output('up_contactor', self.car_id): 0,
            self.mapper.addr_output('down_contactor', self.car_id): 0,
            self.mapper.addr_output('high_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0,
            self.mapper.addr_output('motor_start', self.car_id): 0,
        })

    async def release_brakes(self) -> None:
        """释放所有刹车（设为默认状态 000）

        调用场景：
            - 启动电机前（确保刹车松开让电机能驱动）
            - 手动模式退出后（恢复到默认释放态）
        """
        await self.set_brakes(0, 0, 0)

    async def set_speed(self, high_speed: bool) -> None:
        """切换速度接触器（运行时）"""
        await self.io.set_many({
            self.mapper.addr_output('high_speed_contactor', self.car_id): 1 if high_speed else 0,
            self.mapper.addr_output('low_speed_contactor', self.car_id): 0 if high_speed else 1,
            self.mapper.addr_output('motor_start', self.car_id): 1,
        })

    async def set_brakes(self, b1: int = 0, b2: int = 0, b3: int = 0) -> None:
        """设置刹车（运行时）"""
        await self.io.set_many({
            self.mapper.addr_output('brake_1', self.car_id): b1,
            self.mapper.addr_output('brake_2', self.car_id): b2,
            self.mapper.addr_output('brake_3', self.car_id): b3,
        })

    async def set_brake_level(self, level: int) -> None:
        """8 档刹车（0=释放, 1-7=不同组合）"""
        b1 = 1 if (level & 0b001) else 0
        b2 = 1 if (level & 0b010) else 0
        b3 = 1 if (level & 0b100) else 0
        await self.set_brakes(b1, b2, b3)


class DoorController:
    """门继电器控制器"""

    def __init__(self, io: IOClient, mapper: IOMapper, car_id: int) -> None:
        self.io = io
        self.mapper = mapper
        self.car_id = car_id

    async def open(self) -> None:
        """开门继电器 ON，关门继电器 OFF"""
        await self.io.set_many({
            self.mapper.addr_output('door_open_relay', self.car_id): 1,
            self.mapper.addr_output('door_close_relay', self.car_id): 0,
        })

    async def close(self) -> None:
        """关门继电器 ON，开门继电器 OFF"""
        await self.io.set_many({
            self.mapper.addr_output('door_open_relay', self.car_id): 0,
            self.mapper.addr_output('door_close_relay', self.car_id): 1,
        })

    async def idle(self) -> None:
        """两个继电器都关"""
        await self.io.set_many({
            self.mapper.addr_output('door_open_relay', self.car_id): 0,
            self.mapper.addr_output('door_close_relay', self.car_id): 0,
        })
