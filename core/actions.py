"""
actions.py —— 动作队列（算法层 → 硬件层的桥梁）

设计原则:
    - Action 是高层抽象，不包含任何 IO 地址
    - 算法层 put，硬件层 get
    - 硬件层负责把 Action 展开为具体 IO 序列
"""

import asyncio
from dataclasses import dataclass
from enum import Enum


class ActionKind(Enum):
    """动作类型"""
    INITIALIZE = "initialize"          # 启动定位：跑到初始化段（方向由 config 决定）
    MOVE_UP = "move_up"                # 上行（到 car.target_floor）
    MOVE_DOWN = "move_down"            # 下行
    OPEN_DOOR = "open_door"            # 开门
    CLOSE_DOOR = "close_door"          # 关门
    SET_DISPLAY = "set_display"        # 设置 7 段数码管（需要 floor 参数）
    RESET_FAULT = "reset_fault"        # 复位故障
    EMERGENCY_STOP = "emergency_stop"  # 紧急停止
    NOOP = "noop"                      # 空动作（占位/心跳）
    LIGHT_OFF = "light_off"            # 熄灯
    LIGHT_ON = "light_on"              # 亮灯


@dataclass(frozen=True)
class Action:
    """
    单个动作

    SET_DISPLAY 用 floor 或 glyph 二选一：
        - floor 给定 → 查 display_config.floor_display 映射到字符
        - glyph 给定 → 直接用字符（跳过 floor 映射，比如 'up'/'down'/'fault'）
    MOVE_UP/MOVE_DOWN 由硬件层看 car.target_floor 自动决定停哪层。

    """
    kind: ActionKind
    floor: int | None = None
    glyph: str | None = None

    def __repr__(self) -> str:
        parts = [self.kind.value]
        if self.floor is not None:
            parts.append(f'floor={self.floor}')
        if self.glyph is not None:
            parts.append(f'glyph={self.glyph!r}')
        return f'Action({", ".join(parts)})'


class ActionQueue:
    """
    asyncio.Queue 的轻包装

    提供 qsize() 给 REPL 的 /actions 命令用，
    提供 put_action/get_action 的语义化命名。
    """
    def __init__(self) -> None:
        self._q: asyncio.Queue[Action] = asyncio.Queue()

    async def put(self, action: Action) -> None:
        await self._q.put(action)

    async def get(self) -> Action:
        return await self._q.get()

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()