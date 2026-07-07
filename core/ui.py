"""
ui.py —— 电梯 UI 指示灯控制器

封装所有 UI 类 IO 写操作（满载/故障/照明/风扇/开门指示/轿内按钮灯）。
游戏开发视角:UI 是电梯实体的属性,不是物理动作。

设计原则:
    - 上层只通过 set_xxx(bool) 修改 UI。严禁直接赋值 car.ui.fault = True
      (那只会改逻辑状态不同步 IO)。
    - 读:car.ui.fault / car.ui.cabin_button_leds[floor] 直读
    - 写:app.ui[cid].set_fault(True) —— 同步更新 Car.ui + IO
    - 不自动绑定事件:cabin_button_X 按下不自动亮 LED,
      上层逻辑自行决定(为未来闪灯/复杂效果预留解耦)
    - 单一 IO 写路径:每方法一次 set_many(后续可改成批量 flush,目前够用)

上层调用示例:
    app.ui[1].set_fault(True)             # 1 号梯亮故障灯
    app.ui[1].set_cabin_button_led(3, True) # 1 号梯轿内 3 楼按钮灯亮
    car.ui.fault                           # 读当前逻辑状态
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .io_client import IOClient
    from .io_mapper import IOMapper
    from .player import Car


class UiController:
    """电梯 UI 指示灯控制器:同步 Car.ui 逻辑状态 → IO 输出

    封装单步 set_xxx(bool) 操作,内部一次 IO set_many。
    不自动批量 flush —— 由 IOClient tick 自动合并不同控制器调用。
    """

    def __init__(self, io_write: 'IOClient', mapper: 'IOMapper',
                 car_id: int, car: 'Car') -> None:
        """
        Args:
            io_write: 写入用的 IOClient(per-car 实例,避免 6 部车共享写通道拥堵)
            mapper: IO 地址映射
            car_id: 本控制器归属轿厢 ID
            car: 本控制器对应的 Car 实体(状态写入此处)
        """
        self.io_write = io_write
        self.mapper = mapper
        self.car_id = car_id
        self.car = car

    # ===== 轿厢状态指示灯 =====

    async def set_full_load(self, on: bool) -> None:
        """满载指示灯"""
        self.car.ui.full_load = on
        await self.io_write.set(
            self.mapper.addr_output('full_load_indicator', self.car_id),
            1 if on else 0,
        )

    async def set_fault(self, on: bool) -> None:
        """故障指示灯"""
        self.car.ui.fault = on
        await self.io_write.set(
            self.mapper.addr_output('fault_indicator', self.car_id),
            1 if on else 0,
        )

    async def set_light(self, on: bool) -> None:
        """照明(电梯内灯)"""
        self.car.ui.light = on
        await self.io_write.set(
            self.mapper.addr_output('light_indicator', self.car_id),
            1 if on else 0,
        )

    async def set_fan(self, on: bool) -> None:
        """风扇"""
        self.car.ui.fan = on
        await self.io_write.set(
            self.mapper.addr_output('fan_indicator', self.car_id),
            1 if on else 0,
        )

    # ===== 轿内按钮指示灯 =====

    async def set_cabin_button_led(self, floor: int, on: bool) -> None:
        """轿内 X 楼按钮 LED

        Args:
            floor: 楼层号(1..max_floor)
            on: True=亮, False=灭
        """
        self.car.ui.cabin_button_leds[floor] = on
        await self.io_write.set(
            self.mapper.addr_output(f'cabin_button_led_{floor}', self.car_id),
            1 if on else 0,
        )

    # ===== 批量同步 =====

    async def sync_to_io(self) -> None:
        """把 Car.ui 的当前逻辑状态全量同步到 IO(一次性 set_many)

        调用场景:
            - reset() 重置后,把"全 False"一次性写入 IO(避免多次 tick)
            - /reload 后 Car.ui 还在但 IO 不一定匹配,补一次同步
        """
        writes: dict[str, int] = {}
        sigs: list[tuple[str, bool]] = [
            ('full_load_indicator', self.car.ui.full_load),
            ('fault_indicator', self.car.ui.fault),
            ('light_indicator', self.car.ui.light),
            ('fan_indicator', self.car.ui.fan),
        ]
        for sig, on in sigs:
            try:
                addr = self.mapper.addr_output(sig, self.car_id)
                writes[addr] = 1 if on else 0
            except KeyError:
                continue  # io_config 缺该信号时跳过(不抛)
        # 轿内按钮 LED
        for floor, on in self.car.ui.cabin_button_leds.items():
            try:
                addr = self.mapper.addr_output(
                    f'cabin_button_led_{floor}', self.car_id
                )
                writes[addr] = 1 if on else 0
            except KeyError:
                continue

        if writes:
            await self.io_write.set_many(writes)