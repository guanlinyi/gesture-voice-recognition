"""Dependency and file checks without opening a camera, microphone, or network."""
import importlib.util
import json
import platform
import sys
from .asr import DEFAULT_MODEL, create_recognizer, model_files


def check(directory):
    report = {"python": sys.version.split()[0], "architecture": platform.machine(),
              "dependencies": {name: importlib.util.find_spec(name) is not None
                               for name in ("sherpa_onnx", "numpy", "mediapipe", "cv2", "PIL", "tkinter")}}
    try:
        report["model_files"] = model_files(directory)
    except ValueError as exc:
        report["model_error"] = str(exc)
    report["ready"] = all(report["dependencies"].values()) and "model_files" in report
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2
