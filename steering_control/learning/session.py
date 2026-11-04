"""Persist newly captured media with numeric poses; fit without reopening media."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

import numpy as np

from ..gesture.features import Descriptor, FEATURE_VERSION
from ..gesture.recognition import corrected_prediction
from ..model import SavedGestureModel, DEFAULT_MANIFEST
from ..paths import DATA
from .sampling import SamplingConfig, select_coverage

LABELS = {"OK手势": "OK手势", "握拳": "握拳", "食指竖起": "摆1字", "手掌展开": "手掌展开", "非目标手势": "__background__"}


def identifier():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, separators=(",", ":"))


def pose_record(descriptor, reading):
    return {"descriptor": None if descriptor is None else {
                "space": descriptor.space, "vector": descriptor.vector.tolist(),
                "rotation": None if descriptor.rotation is None else np.asarray(descriptor.rotation).tolist(),
                "scale": descriptor.scale},
            "reading": {key: getattr(reading, key) for key in ("key", "score", "margin", "hand_present")}}


def descriptor_of(row):
    item = row["descriptor"]
    return None if item is None else Descriptor(item["space"], np.asarray(item["vector"]), item.get("rotation"), item.get("scale"))


def expected_label(row):
    return None if row["label"] == "__background__" else row["label"]


def evaluate(library, rows, config):
    usable = [(row, descriptor_of(row)) for row in rows if row["descriptor"] is not None]
    wrong = [index for index, (row, descriptor) in enumerate(usable)
             if corrected_prediction(library, descriptor, row["reading"], config).label != expected_label(row)]
    return {"frames": len(rows), "usable": len(usable), "no_hand": len(rows) - len(usable),
            "errors": len(wrong), "accuracy": None if not usable else 1 - len(wrong) / len(usable),
            "error_frames": [{"item": usable[i][0]["item"], "frame": usable[i][0]["frame"]} for i in wrong]}


def fit(base, rows):
    """Reuse coverage selection, then replay and supplement remaining hard poses."""
    library = deepcopy(base.library)
    selected, groups = set(), {}
    for index, row in enumerate(rows):
        descriptor = descriptor_of(row)
        if descriptor is not None:
            groups.setdefault((row["label"], descriptor.space), []).append((index, descriptor))
    if not groups:
        raise ValueError("参考素材没有有效关键点，请重新采集；无需额外提供图片")
    for (label, space), values in groups.items():
        errors = {index for index, descriptor in values
                  if base.predict(descriptor, rows[index]["reading"]).label != expected_label(rows[index])}
        chosen, _ = select_coverage(values, SamplingConfig(), errors)
        selected.update(values[index][0] for index in chosen)

    buckets = {}
    def add(indices):
        for index in sorted(indices):
            row = rows[index]
            if row["label"] not in buckets:
                demo = {"label": row["label"], "features": []}
                buckets[row["label"]] = demo
                library.demonstrations.append(demo)
            buckets[row["label"]]["features"].append({**deepcopy(row["descriptor"]), "item": row["item"], "frame": row["frame"]})
    add(selected)
    # Correct real observed poses only; never fabricate intermediate hands.
    for _ in range(4):
        remaining = [index for index, row in enumerate(rows) if row["descriptor"] is not None and index not in selected
                     and corrected_prediction(library, descriptor_of(row), row["reading"], base.config).label != expected_label(row)]
        if not remaining or len(selected) >= 1024:
            break
        additions = set(remaining[:1024 - len(selected)])
        add(additions)
        selected.update(additions)
    report = {"method": "error_first_coverage_then_replay_repair", "added_references": len(selected),
              "before": evaluate(base.library, rows, base.config), "after": evaluate(library, rows, base.config),
              "scope": "采集数据回放，不能替代独立验证"}
    return library, report


class LearningSession:
    def __init__(self, base, root=None):
        self.root = Path(root or DATA / "learning")
        self.base = base
        self.lock = threading.RLock()
        self.status, self.error, self.result = "未学习", "", None
        self.busy = False
        self.writer = self.recording = None
        self.latest = None
        self.revision = 0
        self.validation_model = None
        self._draft()

    def _draft(self):
        drafts = self.root / "drafts"
        candidates = sorted(path for path in drafts.glob("*") if path.is_dir() and not path.is_symlink())
        self.directory = candidates[-1] if candidates else drafts / identifier()
        self.directory.mkdir(parents=True, exist_ok=True)
        excluded = set()
        for path in (self.directory / "output_json").glob("excluded_*.json"):
            excluded.update(json.loads(path.read_text())["ids"])
        self.items = [json.loads(path.read_text()) for path in sorted((self.directory / "input_json").glob("*.json"))]
        self.items = [item for item in self.items if item["id"] not in excluded]
        self.revision += 1

    def choices(self):
        paths = [DEFAULT_MANIFEST, self.base.manifest_path]
        paths += sorted((self.root / "models").glob("*/output_json/model.json"))
        paths += sorted((self.root / "models").glob("*/output_json/*/model.json"))
        excluded = set()
        for record in (self.root / "excluded_models/output_json").glob("*.json"):
            excluded.add(json.loads(record.read_text())["manifest"])
        return [path for path in dict.fromkeys(Path(path).resolve() for path in paths) if str(path) not in excluded]

    def exclude_model(self, path):
        self._idle()
        write_json(self.root / "excluded_models/output_json" / (identifier() + ".json"),
                   {"manifest": str(Path(path).resolve())})

    def validate(self):
        self._idle()
        model = self.validation_model or self.base
        rows = self.rows("validation")
        if not rows:
            raise ValueError("请先用 T 录制独立验证录像")
        return model.name, evaluate(model.library, rows, model.config)

    def set_base(self, model):
        with self.lock:
            self._idle()
            self.base, self.validation_model = model, None
            self.result = None
            self.status, self.error = "未学习", ""
            self.revision += 1

    def _idle(self):
        if self.busy or self.recording is not None:
            raise ValueError("请等待学习完成或先结束录像")

    def on_frame(self, frame, descriptor, reading, fps=15):
        with self.lock:
            self.latest = (frame.copy(), pose_record(descriptor, reading), max(1., min(60., fps)))
            if self.recording is not None:
                if len(self.recording["rows"]) >= 18000:
                    self.stop_video()
                    return
                self.writer.write(frame)
                self.recording["rows"].append(pose_record(descriptor, reading))

    def _label(self, label):
        if label not in LABELS:
            raise ValueError("请选择采集标签")
        return LABELS[label]

    def photograph(self, label):
        import cv2
        with self.lock:
            self._idle()
            if self.latest is None or self.latest[1]["descriptor"] is None:
                raise ValueError("当前没有有效手部关键点")
            frame, row, _ = self.latest
            item = {"id": identifier(), "kind": "image", "split": "reference", "label": self._label(label), "rows": [deepcopy(row)]}
            item["file"] = "input_png/" + item["id"] + ".png"
            path = self.directory / item["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(path), frame):
                raise RuntimeError("图片保存失败")
            self._register(item)
            return item

    def start_video(self, label, split="reference"):
        import cv2
        with self.lock:
            self._idle()
            if self.latest is None:
                raise ValueError("尚未收到摄像头画面")
            if split not in ("reference", "validation"):
                raise ValueError("录像类型无效")
            frame, _, fps = self.latest
            item = {"id": identifier(), "kind": "video", "split": split, "label": self._label(label), "rows": [], "fps": fps}
            item["file"] = "input_mp4/" + item["id"] + ".mp4"
            path = self.directory / item["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (frame.shape[1], frame.shape[0]))
            if not writer.isOpened():
                writer.release()
                raise RuntimeError("无法创建 MP4 录像")
            self.writer, self.recording = writer, item
            self.revision += 1

    def stop_video(self):
        with self.lock:
            if self.recording is None:
                return None
            item, self.recording = self.recording, None
            self.writer.release()
            self.writer = None
            if not item["rows"]:
                raise ValueError("未录到帧，请重新录制")
            self._register(item)
            return item

    def _register(self, item):
        write_json(self.directory / "input_json" / (item["id"] + ".json"), item)
        self.items.append(item)
        self.status, self.result = "未学习", None
        self.error = ""
        self.revision += 1

    def exclude(self, ids):
        with self.lock:
            self._idle()
            ids = set(ids)
            if not ids or not ids <= {item["id"] for item in self.items}:
                raise ValueError("请选择本批素材")
            # Logical deletion: retain captured originals, exclude them from training.
            write_json(self.directory / "output_json" / ("excluded_" + identifier() + ".json"), {"ids": sorted(ids)})
            self.items = [item for item in self.items if item["id"] not in ids]
            self.status, self.result = "未学习", None
            self.error = ""
            self.revision += 1

    def rows(self, split):
        return [{**row, "item": item["id"], "frame": index, "label": item["label"]}
                for item in self.items if item["split"] == split for index, row in enumerate(item["rows"])]

    def save(self, name):
        """Called on Tk thread; numerical fitting runs off the UI thread."""
        with self.lock:
            self._idle()
            name = name.strip()
            if not re.fullmatch(r"[^\W_][\w -]{0,63}", name):
                raise ValueError("模型名称为 1–64 个文字、数字、空格或短横线，不以空格/下划线开头")
            destination = self.root / "models" / name
            if destination.exists():
                raise ValueError("已有同名模型，请换一个名称")
            if not self.rows("reference"):
                raise ValueError("请先采集参考图片或 V 参考录像；T 验证录像不参与学习")
            self.busy, self.status, self.error = True, "学习中", ""
            self.worker = threading.Thread(target=self._save, args=(name, destination), daemon=True, name="reference-learning")
            self.worker.start()

    def _save(self, name, destination):
        try:
            rows = self.rows("reference")
            library, report = fit(self.base, rows)
            report["validation"] = evaluate(library, self.rows("validation"), self.base.config)
            anchors = [{"item": "base", "frame": i, "label": d["label"], "descriptor": f,
                        "reading": {"key": "unknown", "score": 0., "margin": 0., "hand_present": True}}
                       for d in self.base.library.demonstrations for i, f in enumerate(d["features"])]
            report["base_reference_replay"] = evaluate(library, anchors, self.base.config)
            record = json.loads(self.base.manifest_path.read_text())
            build = "output_json/" + identifier()
            record.update(name=name, library="reference_library.json",
                          parent={"name": self.base.name, "manifest": str(self.base.manifest_path.resolve()),
                                  "sha256": hashlib.sha256(self.base.manifest_path.read_bytes()).hexdigest()},
                          training={"items": [{key: item[key] for key in ("id", "file", "kind", "split", "label")} for item in self.items],
                                    "report": "learning_report.json", "directory": "../.."})
            # Every saved artifact has a fresh path, and no model is overwritten.
            write_json(self.directory / build / "reference_library.json", {
                "schema": 1, "feature_version": FEATURE_VERSION, "method": "nearest_reference",
                "threshold": library.threshold, "margin": library.margin, "demonstrations": library.demonstrations})
            write_json(self.directory / build / "learning_report.json", report)
            write_json(self.directory / build / "model.json", record)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError("保存前发现同名模型，保留本批，请换一个名称")
            self.directory.rename(destination)
            with self.lock:
                self.result = destination / build / "model.json"
                self.report = report
                self._draft()
                self.status = "已学习"
        except Exception as exc:
            with self.lock:
                self.status, self.error = "学习失败", str(exc)
        finally:
            with self.lock:
                self.busy = False
                self.revision += 1

    def close(self):
        try:
            self.stop_video()
        except ValueError:
            pass  # Closing immediately after record-start may have zero frames.
        worker = getattr(self, "worker", None)
        if worker and worker.is_alive():
            worker.join()  # finish publishing before the process exits
