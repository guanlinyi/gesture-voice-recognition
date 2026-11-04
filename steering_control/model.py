"""Load the shipped numerical references and verify the MediaPipe network."""
import hashlib
import json
from pathlib import Path

from .paths import ROOT, GESTURE_MODEL
from .gesture.features import SampleLibrary
from .gesture.policy import ModelPolicy
from .gesture.recognition import corrected_prediction

DEFAULT_MANIFEST = ROOT / "assets/output_json/model.json"


class SavedGestureModel:
    def __init__(self, manifest=DEFAULT_MANIFEST):
        self.manifest_path = Path(manifest).resolve()
        record = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if record.get("schema") != 1 or record.get("kind") != "ok":
            raise ValueError("需要 OK 准备流程的模型清单")
        self.model_path = GESTURE_MODEL
        if not self.model_path.is_file():
            raise ValueError("缺少 MediaPipe 模型，请先运行 python prepare.py gesture")
        if hashlib.sha256(self.model_path.read_bytes()).hexdigest() != record["network"]["sha256"]:
            raise ValueError("MediaPipe 模型 SHA256 不匹配")
        self.library_path = (self.manifest_path.parent / record["library"]).resolve()
        if not self.library_path.is_relative_to(self.manifest_path.parent):
            raise ValueError("数值参考库必须位于模型清单目录内")
        self.library = SampleLibrary.load(self.library_path)
        self.config = record["work_config"]
        self.classifier_options = {"use_geometric_ok": self.config["classifier"]["ok_geometry"]}
        workflow = record.get("workflow", {})
        self.policy = ModelPolicy(record["kind"], self.config, timing=workflow.get("timing"),
                                  labels=self.library.labels,
                                  reference_ready_priority=workflow.get("reference_ready_priority", False))
        self.name = record["name"]

    def predict(self, descriptor, reading):
        return corrected_prediction(self.library, descriptor, reading, self.config)
