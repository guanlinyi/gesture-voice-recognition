"""MediaPipe 手势分类器：一帧进，单帧读数出。

这里只负责“这一帧像什么”，不负责停留、复位或业务含义；
那些在 GestureGate 和插件里做。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import mediapipe as mp

from .geometry import is_ok_geometry
from .types import GestureReading

MODEL_CATEGORY = {
    "Closed_Fist": "fist",
    "Open_Palm": "palm",
    "Pointing_Up": "one",
    "Victory": "victory",
    "OK": "ok",
    "Thumb_Up": "thumb_up",
    "Thumb_Down": "thumb_down",
    "ILoveYou": "love",
}


def select_hand_index(hand_landmarks, prefer_largest: bool = False) -> int | None:
    if not hand_landmarks:
        return None
    if prefer_largest and len(hand_landmarks) > 1:
        def area(landmarks):
            xs = [p.x for p in landmarks]
            ys = [p.y for p in landmarks]
            return (max(xs) - min(xs)) * (max(ys) - min(ys))

        return max(range(len(hand_landmarks)), key=lambda i: area(hand_landmarks[i]))
    return 0


class GestureClassifier:
    def __init__(
        self,
        model_path: str | Path,
        num_hands: int = 1,
        min_hand_detection_confidence: float = 0.5,
        min_hand_presence_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        use_geometric_ok: bool = True,
        prefer_largest_hand: bool = False,
    ):
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(f"缺少手势模型：{model_path}")

        options = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_hands=num_hands,
            min_hand_detection_confidence=min_hand_detection_confidence,
            min_hand_presence_confidence=min_hand_presence_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )
        self._recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(options)
        self._last_ts = -1
        self.use_geometric_ok = use_geometric_ok
        self.prefer_largest_hand = prefer_largest_hand

    def read(self, frame_bgr, timestamp_ms: int, now: float) -> tuple[GestureReading, object | None]:
        """返回 (单帧读数, 第一只手的 21 个关键点或 None)。"""
        timestamp_ms = max(self._last_ts + 1, int(timestamp_ms))
        self._last_ts = timestamp_ms

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._recognizer.recognize_for_video(mp_image, timestamp_ms)

        if not result.hand_landmarks:
            return GestureReading("none", 0.0, 0.0, False, now), None

        index = select_hand_index(result.hand_landmarks, self.prefer_largest_hand)
        if index is None:
            return GestureReading("none", 0.0, 0.0, False, now), None

        landmarks = result.hand_landmarks[index]
        world_landmarks = (
            result.hand_world_landmarks[index]
            if result.hand_world_landmarks and index < len(result.hand_world_landmarks)
            else None
        )

        handedness = "unknown"
        if result.handedness and index < len(result.handedness) and result.handedness[index]:
            handedness = result.handedness[index][0].category_name or "unknown"

        categories = result.gestures[index] if result.gestures and index < len(result.gestures) else []
        if categories:
            top = categories[0]
            key = MODEL_CATEGORY.get(top.category_name, "unknown")
            score = float(top.score)
            margin = score - float(categories[1].score) if len(categories) > 1 else score
        else:
            key, score, margin = "unknown", 0.0, 0.0

        # 模型不确定时的 OK 几何兜底；模型很确信是别的手型时不覆盖。
        # 0.75 是让兜底结果能过闸门的启发式分数，不是校准概率。
        if self.use_geometric_ok and score < 0.85:
            height, width = frame_bgr.shape[:2]
            if is_ok_geometry(landmarks, width, height):
                key = "ok"
                score = max(score, 0.75)
                margin = max(margin, 0.75)

        return GestureReading(key, score, max(0.0, margin), True, now, handedness, world_landmarks), landmarks

    def close(self) -> None:
        self._recognizer.close()

    def __enter__(self) -> "GestureClassifier":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
