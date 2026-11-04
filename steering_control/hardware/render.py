"""实时骨架绘制。"""
import cv2

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]

def draw_hand(frame, landmarks) -> None:
    if landmarks is None:
        return
    height, width = frame.shape[:2]
    points = [(int(p.x * width), int(p.y * height)) for p in landmarks]
    for a, b in HAND_CONNECTIONS:
        cv2.line(frame, points[a], points[b], (60, 220, 255), 2)
    for point in points:
        cv2.circle(frame, point, 3, (255, 120, 200), -1)
