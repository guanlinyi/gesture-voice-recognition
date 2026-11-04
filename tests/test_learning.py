"""Learning and page boundaries using synthetic poses; no saved media read."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from steering_control.confirmation import Confirmation
from steering_control.gesture.features import SampleLibrary, Descriptor
from steering_control.gesture.recognition import corrected_prediction
from steering_control.learning.session import LearningSession, evaluate, fit, write_json
from steering_control.model import DEFAULT_MANIFEST, SavedGestureModel


def row(value=.8, label="握拳", item="synthetic", frame=0):
    return {"descriptor": {"space": "world", "vector": [value] * 210, "rotation": None, "scale": .1},
            "reading": {"key": "palm", "score": .95, "margin": .9, "hand_present": True},
            "label": label, "item": item, "frame": frame}


class Base:
    def __init__(self, folder):
        record = json.loads(DEFAULT_MANIFEST.read_text())
        self.manifest_path = folder / "base/output_json/model.json"
        write_json(self.manifest_path, record)
        self.name = "synthetic-base"
        self.config = record["work_config"]
        self.library = SampleLibrary(threshold=.12, margin=.001)
        self.library.demonstrations = [{"label": "OK手势", "features": [{"space": "world", "vector": [1.] * 210}]}]
    def predict(self, descriptor, reading):
        return corrected_prediction(self.library, descriptor, reading, self.config)


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = Base(self.root)
        self.session = LearningSession(self.base, self.root / "learning")

    def item(self, split="reference", label="握拳", count=12):
        rows = [row(label=label, frame=i) for i in range(count)]
        return {"id": split + label, "kind": "video", "file": "input_mp4/not-read.mp4", "label": label,
                "split": split, "rows": [{k: r[k] for k in ("descriptor", "reading")} for r in rows]}

    def test_repeated_poses_need_one_reference_and_base_is_unchanged(self):
        before = deepcopy(self.base.library.demonstrations)
        library, report = fit(self.base, [row(frame=i) for i in range(100)])
        self.assertEqual(report["added_references"], 1)
        self.assertEqual(report["before"]["errors"], 100)
        self.assertEqual(report["after"]["errors"], 0)
        self.assertEqual(self.base.library.demonstrations, before)
        self.assertEqual(library.predict(Descriptor("world", np.full(210, 1.))).label, "OK手势")

    def test_video_only_save_records_parent_and_resets_batch_without_enabling(self):
        self.session._register(self.item())
        self.session._register(self.item("validation", label="手掌展开"))
        draft = self.session.directory
        self.session.save("新参考模型")
        self.session.worker.join(8)
        self.assertFalse(self.session.busy)
        self.assertEqual(self.session.status, "已学习", self.session.error)
        manifest = self.session.result
        saved = json.loads(manifest.read_text())
        self.assertEqual(saved["parent"]["name"], self.base.name)
        self.assertEqual(saved["parent"]["sha256"], hashlib.sha256(self.base.manifest_path.read_bytes()).hexdigest())
        self.assertEqual(self.session.items, [])
        self.assertNotEqual(self.session.directory, draft)
        self.assertIs(self.session.base, self.base)
        self.assertIn(manifest, self.session.choices())
        library = SampleLibrary.load(manifest.parent / saved["library"])
        self.assertNotIn("手掌展开", [d["label"] for d in library.demonstrations])
        self.assertEqual(self.session.report["validation"]["errors"], 12)
        # Validate the actual loader against a synthetic network, without source media.
        network = self.root / "network.task"
        network.write_bytes(b"synthetic-network")
        with patch("steering_control.model.GESTURE_MODEL", network), patch("steering_control.model.hashlib.sha256") as digest:
            digest.return_value.hexdigest.return_value = saved["network"]["sha256"]
            loaded = SavedGestureModel(manifest)
        self.assertEqual(loaded.name, "新参考模型")
        self.assertEqual(loaded.predict(Descriptor("world", np.full(210, .8)), row()["reading"]).label, "握拳")

    def test_excluded_video_is_not_learned_and_exclusion_survives_restart(self):
        item = self.item()
        self.session._register(item)
        metadata = self.session.directory / "input_json" / (item["id"] + ".json")
        self.session.exclude([item["id"]])
        self.assertTrue(metadata.is_file())
        restored = LearningSession(self.base, self.session.root)
        self.assertEqual(restored.items, [])
        with self.assertRaises(ValueError):
            restored.save("空模型")

    def test_validation_only_and_missing_hands_are_explicit(self):
        self.session._register(self.item("validation"))
        with self.assertRaises(ValueError):
            self.session.save("只有验证")
        valid, missing = row(), row()
        missing["descriptor"] = None
        report = evaluate(self.base.library, [valid, missing], self.base.config)
        self.assertEqual((report["frames"], report["usable"], report["no_hand"]), (2, 1, 1))
        with self.assertRaises(ValueError):
            fit(self.base, [missing])

    def test_new_selected_base_is_the_saved_parent_and_duplicate_names_are_rejected(self):
        alternate = Base(self.root / "alternate")
        alternate.name = "另一个基础模型"
        self.session.set_base(alternate)
        self.session._register(self.item())
        self.session.save("同名保护")
        self.session.worker.join(8)
        self.assertEqual(self.session.status, "已学习", self.session.error)
        self.assertEqual(json.loads(self.session.result.read_text())["parent"]["name"], alternate.name)
        with self.assertRaises(ValueError):
            self.session.save("同名保护")
        with self.assertRaises(ValueError):
            self.session.save("../越界")

    def test_capture_tabs_and_validation_never_require_opening_a_video(self):
        self.session._register(self.item("validation"))
        name, report = self.session.validate()
        self.assertEqual(name, self.base.name)
        self.assertEqual(report["frames"], 12)
        self.session.recording = {"rows": []}
        with self.assertRaises(ValueError):
            self.session.set_base(self.base)
        with self.assertRaises(ValueError):
            self.session.exclude([self.session.items[0]["id"]])
        self.session.recording = None

    def test_background_references_are_scored_as_rejection(self):
        library, report = fit(self.base, [row(label="__background__")])
        self.assertEqual(report["after"]["errors"], 0)
        self.assertIsNone(library.predict(Descriptor("world", np.full(210, .8))).label)

    def test_captured_image_is_raw_and_unsaved_draft_survives_restart(self):
        frame = np.zeros((12, 12, 3), dtype=np.uint8)
        reading = SimpleNamespace(**row()["reading"])
        self.session.on_frame(frame, Descriptor("world", np.full(210, .8)), reading)
        frame[:] = 255  # Simulate drawing on the displayed frame afterwards.
        with patch("cv2.imwrite", return_value=True) as writing:
            item = self.session.photograph("握拳")
        self.assertTrue(np.all(writing.call_args.args[1] == 0))
        restored = LearningSession(self.base, self.session.root)
        self.assertEqual([x["id"] for x in restored.items], [item["id"]])


class ModeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.control = Confirmation(self.actions.append, model_name="test",
            prompts=SimpleNamespace(status=lambda: {"ready": True, "error": None}, get=lambda label: (b"wav", .1)), clock=lambda: 100.)
        self.control.connect("phone")
        self.control.ready("phone")
        self.control.camera_alive()

    def test_learning_blocks_gesture_voice_old_model_result_and_ready_race(self):
        old = self.control.snapshot()["input"]["token"]
        self.assertTrue(self.control.observe("握拳"))
        pending = self.control.snapshot()["pending"]
        self.control.set_learning(True)
        self.control.ready("phone")
        self.assertFalse(self.control.observe("手掌展开"))
        self.control.direct_transcript("phone", old, SimpleNamespace(type="final", text="方向盘展开"))
        self.assertFalse(self.control.submit_voice_intent("phone", old, "FOLD", "收起"))
        self.control.transcript("phone", pending["id"], SimpleNamespace(type="final", text="是的"))
        self.assertEqual(self.actions, [])
        self.assertEqual(self.control.snapshot()["input"]["mode"], "muted")
        self.control.set_learning(False)
        self.assertNotEqual(self.control.snapshot()["input"]["token"], old)
        self.control.direct_transcript("phone", old, SimpleNamespace(type="final", text="方向盘展开"))
        self.assertEqual(self.actions, [])
        new = self.control.snapshot()["input"]["token"]
        self.control.direct_transcript("phone", new, SimpleNamespace(type="final", text="方向盘展开"))
        self.assertEqual([event["id"] for event in self.actions], [3])

    def test_mode_change_invalidates_a_reserved_but_not_sent_action(self):
        token = self.control.snapshot()["input"]["token"]
        old_dispatch = self.control._dispatch
        with patch.object(self.control, "_dispatch", side_effect=lambda p, a: (self.control.set_learning(True), old_dispatch(p, a))[-1]):
            self.control.submit_voice_intent("phone", token, "FOLD", "折叠")
        self.assertEqual(self.actions, [])

    def test_transition_waits_for_an_already_started_dispatch(self):
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        self.control.dispatch = lambda action: (entered.set(), release.wait(3))
        token = self.control.snapshot()["input"]["token"]
        worker = threading.Thread(target=lambda: self.control.submit_voice_intent("phone", token, "FOLD", "折叠"))
        worker.start()
        self.assertTrue(entered.wait(1))
        transition = threading.Thread(target=lambda: (self.control.set_learning(True), switched.set()))
        transition.start()
        self.assertFalse(switched.wait(.05))
        release.set()
        worker.join(2)
        transition.join(2)
        self.assertTrue(switched.is_set())
        self.assertEqual(self.control.snapshot()["input"]["mode"], "muted")
