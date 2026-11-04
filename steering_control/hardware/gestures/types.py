"""手势数据对象：输入读数与确认事件。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class GestureReading:
    """单帧读数；key 是内部手型名，score 是 top1 置信度，margin 是 top1-top2。

    handedness 只用于显示和排查，不参与闸门或映射：左手和右手做同一手势等价。
    """

    key: str
    score: float
    margin: float
    hand_present: bool
    t: float
    handedness: str = "unknown"
    world_landmarks: object | None = None


@dataclass(frozen=True)
class AcceptedGesture:
    """通过置信度、停留和复位检查后的一次手势事件。"""

    key: str
    score: float
    t: float
    seq: int
