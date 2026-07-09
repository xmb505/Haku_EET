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
    - 事件驱动:每次 set_xxx 后通知注册的 observer,供 debug 监视器消费
      (不依赖轮询,对齐项目事件驱动哲学)

上层调用示例:
    app.ui[1].set_fault(True)             # 1 号梯亮故障灯
    app.ui[1].set_cabin_button_led(3, True) # 1 号梯轿内 3 楼按钮灯亮
    car.ui.fault                           # 读当前逻辑状态
"""

from __future__ import annotations

from typing import Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .io_client import IOClient
    from .io_mapper import IOMapper
    from .player import Car

UiObserver = Callable[[int, str, object], Awaitable[None]]


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
        self._observers: list[UiObserver] = []

    def add_observer(self, cb: UiObserver) -> None:
        """注册 UI 写观测器（事件驱动：每次 set_xxx 后调用 cb(car_id, signal_name, value)）"""
        self._observers.append(cb)

    def remove_observer(self, cb: UiObserver) -> None:
        """移除观测器"""
        try:
            self._observers.remove(cb)
        except ValueError:
            pass

    async def _notify_observers(self, signal: str, value: object) -> None:
        for cb in self._observers:
            try:
                await cb(self.car_id, signal, value)
            except Exception:
                pass

    def _addr(self, signal: str) -> str | None:
        """查信号地址，缺信号时打印警告返回 None"""
        try:
            return self.mapper.addr_output(signal, self.car_id)
        except KeyError:
            print(f'[ui:car{self.car_id}] 信号 {signal} 未在 io_config 中配置，跳过')
            return None

    # ===== 轿厢状态指示灯 =====

    async def set_full_load(self, on: bool) -> None:
        """满载指示灯"""
        self.car.ui.full_load = on
        addr = self._addr('full_load_indicator')
        if addr is not None:
            await self.io_write.set(addr, 1 if on else 0)
        await self._notify_observers('full_load', on)

    async def set_fault(self, on: bool) -> None:
        """故障指示灯"""
        self.car.ui.fault = on
        addr = self._addr('fault_indicator')
        if addr is not None:
            await self.io_write.set(addr, 1 if on else 0)
        await self._notify_observers('fault', on)

    async def set_light(self, on: bool) -> None:
        """照明(电梯内灯)"""
        self.car.ui.light = on
        addr = self._addr('light_indicator')
        if addr is not None:
            await self.io_write.set(addr, 1 if on else 0)
        await self._notify_observers('light', on)

    async def set_fan(self, on: bool) -> None:
        """风扇"""
        self.car.ui.fan = on
        addr = self._addr('fan_indicator')
        if addr is not None:
            await self.io_write.set(addr, 1 if on else 0)
        await self._notify_observers('fan', on)

    # ===== 轿内按钮指示灯 =====

    async def set_cabin_button_led(self, floor: int, on: bool) -> None:
        """轿内 X 楼按钮 LED

        Args:
            floor: 楼层号(1..max_floor)
            on: True=亮, False=灭
        """
        self.car.ui.cabin_button_leds[floor] = on
        addr = self._addr(f'cabin_button_led_{floor}')
        if addr is not None:
            await self.io_write.set(addr, 1 if on else 0)
        await self._notify_observers(f'cabin_led_{floor}', on)

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
            addr = self._addr(sig)
            if addr is not None:
                writes[addr] = 1 if on else 0
        # 轿内按钮 LED
        for floor, on in self.car.ui.cabin_button_leds.items():
            addr = self._addr(f'cabin_button_led_{floor}')
            if addr is not None:
                writes[addr] = 1 if on else 0

        if writes:
            await self.io_write.set_many(writes)