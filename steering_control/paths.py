"""Portable defaults; optional local overrides never belong in Git."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_file = ROOT / "config.local.json"
_local = json.loads(_file.read_text(encoding="utf-8")) if _file.is_file() else {}
if not isinstance(_local, dict):
    raise ValueError("config.local.json 必须为 JSON 对象")


def setting(name, default=None):
    """STEERING_<NAME> overrides config.local.json; relative paths use repo root."""
    value = os.environ.get("STEERING_" + name.upper()) or _local.get(name) or default
    if value and (name.endswith("_model") or name in ("data_dir", "llama_server", "camera_init_script")):
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else ROOT / path)
    return value


DATA = Path(setting("data_dir", str(ROOT / "data")))
GESTURE_MODEL = Path(setting("gesture_model", str(DATA / "gesture/input_task/gesture_recognizer.task")))
