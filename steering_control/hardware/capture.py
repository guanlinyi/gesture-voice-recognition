"""输入源：DroidCam HTTP 流、本地视频或摄像头。"""
from __future__ import annotations

from pathlib import Path
import sys

import cv2


def open_source(video: str | None = None, stream: str | None = None, camera: int = 0, backend: str = "auto"):
    if stream:
        # 延迟导入，避免只跑视频时还依赖 urllib 分支。
        from steering_control.hardware.droidcam_stream import DroidCamStream

        return DroidCamStream(stream)

    if video:
        path = Path(video)
        if not path.exists():
            raise FileNotFoundError(f"视频文件不存在：{path}")
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频：{path}")
        return cap

    backends = {
        "msmf": getattr(cv2, "CAP_MSMF", cv2.CAP_ANY),
        "dshow": getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY),
        "v4l2": getattr(cv2, "CAP_V4L2", cv2.CAP_ANY),
        "any": cv2.CAP_ANY,
    }
    if backend == "auto":
        candidates = ["msmf", "dshow"] if sys.platform == "win32" else ["v4l2", "any"]
    else:
        candidates = [backend]
    for name in candidates:
        cap = cv2.VideoCapture(int(camera), backends[name])
        if cap.isOpened():
            return cap
        cap.release()
    raise RuntimeError("无法打开摄像头。请关闭其他相机应用，或更换 --camera 编号。")


def get_fps(cap, default: float = 30.0) -> float:
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS))
    except Exception:
        return default
    if not fps or fps != fps or fps <= 1:
        return default
    return fps
