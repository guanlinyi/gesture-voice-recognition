"""关键点几何兜底：模型没把握时，用 21 点判断 OK。

只做 OK 这一种，因为它是工作线里最容易被模型漏掉的手型。
这是启发式规则，不是校准过的概率；只作为模型不确定时的补充。
"""
from __future__ import annotations

import math

THUMB_TIP = 4
INDEX_TIP = 8
INDEX_DIP = 7
INDEX_PIP = 6
INDEX_MCP = 5
MIDDLE = (12, 10, 9)
RING = (16, 14, 13)
PINKY = (20, 18, 17)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _extended(points, tip: int, pip: int, mcp: int) -> bool:
    return _dist(points[tip], points[mcp]) > _dist(points[pip], points[mcp]) * 1.05


def is_ok_geometry(landmarks, width: int, height: int) -> bool:
    """拇指和食指成圈、其余三指伸直、食指不伸直时，判为 OK。"""
    if landmarks is None or len(landmarks) < 21:
        return False

    points = [(lm.x * width, lm.y * height) for lm in landmarks]
    scale = sum(_dist(points[i], points[0]) for i in (5, 9, 13, 17)) / 4.0
    if scale < max(15.0, min(width, height) * 0.035):
        return False

    # 拇指尖和食指尖要足够近；用食指末节长度做尺度，避免绝对像素阈值。
    if _dist(points[THUMB_TIP], points[INDEX_TIP]) >= _dist(points[INDEX_DIP], points[INDEX_TIP]) * 1.2:
        return False

    if not all(_extended(points, *finger) for finger in (MIDDLE, RING, PINKY)):
        return False
    if _extended(points, INDEX_TIP, INDEX_PIP, INDEX_MCP):
        return False

    return True
