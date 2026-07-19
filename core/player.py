"""
player.py —— 电梯"玩家"抽象

只保留现实状态：位置、方向、门、故障、显示。
算法层只 import 这个模块，看不到 IO 地址。
"""

from dataclasses import dataclass, field
from enum import Enum


class CarState(Enum):
    """轿厢整体状态机"""
    UNKNOWN = "unknown"   # 启动未初始化，需要 INITIALIZE
    READY = "ready"       # 已初始化，可正常调度
    FAULT = "fault"       # 故障，停止响应任务


class Direction(Enum):
    """轿厢运行方向"""
    IDLE = "idle"
    UP = "up"
    DOWN = "down"


class DoorState(Enum):
    """门状态机"""
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"


@dataclass(frozen=True)
class FaultFlags:
    """故障/保护信号集合（任意一项为 True 都可能影响算法决策）"""
    overload: bool = False           # 超重
    service_mode: bool = False      # 检修模式
    light_curtain: bool = False     # 光幕触发（防夹）
    top_limit: bool = False         # 上端站限位
    bottom_limit: bool = False      # 下端站限位
    door: bool = False              # 关门超时/异常

    def any_active(self) -> bool:
        return any((
            self.overload,
            self.service_mode,
            self.light_curtain,
            self.top_limit,
            self.bottom_limit,
            self.door,
        ))


@dataclass
class IndicatorState:
    """电梯 UI 指示灯逻辑状态（与 IO 输出解耦的游戏实体属性）

    读:  car.ui.fault / car.ui.light / ...
    写:  app.ui[cid].set_fault(True) / ...
    严禁直接赋值 car.ui.fault = True —— 那只会改逻辑状态不同步 IO。
    """
    full_load: bool = False       # 满载指示灯
    fault: bool = False           # 故障指示灯
    light: bool = False           # 照明
    fan: bool = False             # 风扇
    cabin_button_leds: dict[int, bool] = field(default_factory=dict)  # 轿内按钮 LED：floor → on


@dataclass
class Car:
    """
    电梯 = 玩家（首版单实例）

    算法层唯一能看到的状态对象。绝对不包含 IO 地址。
    """
    car_id: int
    state: CarState = CarState.UNKNOWN
    position: int | None = None              # None 表示位置未知（未初始化）
    direction: Direction = Direction.IDLE
    door_state: DoorState = DoorState.CLOSED
    target_floor: int | None = None
    fault: FaultFlags = field(default_factory=FaultFlags)
    display: int = 1                         # 7 段显示的楼层数字
    manual_speed: bool | None = None         # 手动模式当前速度档 (True=高速, False=低速, None=未在动)
    human_presence: int = -1                 # -1=确定无人, 0=不确定, 1=确定有人
    last_dispatch_direction: Direction = Direction.IDLE  # 最后一次外召派车方向（用于 compile 排序）
    ui: IndicatorState = field(default_factory=IndicatorState)  # UI 指示灯逻辑状态
    # 重量三态机属性（小脑维护）
    weight_kg: int = 0                      # 当前载重（kg，最后一次 read_word 结果）
    weight_state: int = 0                   # 0=正常 / 1=临界 / 2=超重
    max_weight: int = 0                     # 配置的最大载重（kg）
    weight_threshold_kg: int = 0            # 临界阈值（kg，已计算 = max_weight * threshold）
    driver_mode: bool = False               # 司机模式:忽略外呼,不自动关门,仅关门按钮关门

    def is_ready(self) -> bool:
        return self.state == CarState.READY and not self.fault.service_mode

    def snapshot(self) -> dict:
        """给 REPL /status 用的快照（可序列化）"""
        return {
            'car_id': self.car_id,
            'state': self.state.value,
            'position': self.position,
            'direction': self.direction.value,
            'door_state': self.door_state.value,
            'target_floor': self.target_floor,
            'display': self.display,
            'human_presence': self.human_presence,
            'fault': {
                'overload': self.fault.overload,
                'service_mode': self.fault.service_mode,
                'light_curtain': self.fault.light_curtain,
                'top_limit': self.fault.top_limit,
                'bottom_limit': self.fault.bottom_limit,
                'driver_mode': self.driver_mode,
            },
            'ui': {
                'full_load': self.ui.full_load,
                'fault': self.ui.fault,
                'light': self.ui.light,
                'fan': self.ui.fan,
                'cabin_button_leds': dict(self.ui.cabin_button_leds),
            },
            'weight_kg': self.weight_kg,
            'weight_state': self.weight_state,
            'max_weight': self.max_weight,
        }

    def __repr__(self) -> str:
        pos = f'L{self.position}' if self.position is not None else '?'
        return (
            f'Car(id={self.car_id}, state={self.state.value}, pos={pos}, '
            f'dir={self.direction.value}, door={self.door_state.value}, '
            f'display={self.display})'
        )