"""Numerical/state tests only; no saved images, videos, microphone or actuator."""
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import hashlib
import json
import tempfile

PACKAGE = "steering_control"
core = import_module(PACKAGE + ".confirmation")
ROOT = Path(__file__).resolve().parents[1]


class Prompts:
    def status(self):
        return {"ready": True, "error": None}

    def get(self, label):
        return b"RIFF-synthetic", .2


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.actions, self.events = [], []
        self.control = core.Confirmation(self.actions.append, model_name="synthetic", prompts=Prompts(),
                                         emit=self.events.append, clock=lambda: self.now)
        self.control.connect("phone")
        self.control.ready("phone")
        self.control.camera_alive()

    def advance(self, seconds, *, camera=True, audio=True):
        self.now += seconds
        if audio:
            self.control.heartbeat("phone")
        if camera:
            self.control.camera_alive()

    def request(self, label="握拳"):
        self.assertTrue(self.control.observe(label))
        return self.control.snapshot()["pending"]

    def listen(self, pending):
        self.control.prompt_audio("phone", pending["id"])
        self.advance(.7)
        self.control.listen("phone", pending["id"], pending["playback_token"])

    def answer(self, pending, text, kind="final"):
        self.control.transcript("phone", pending["id"], SimpleNamespace(text=text, type=kind))

    def test_gesture_only_requests_and_never_dispatches(self):
        pending = self.request()
        self.assertEqual(pending["prompt"], "您是想要进行方向盘的折叠操作吗？")
        self.assertEqual(self.actions, [])

    def test_yes_before_prompt_or_partial_yes_cannot_execute(self):
        pending = self.request()
        self.answer(pending, "是的")
        self.listen(pending)
        self.answer(pending, "是的", "partial")
        self.assertEqual(self.actions, [])
        self.answer(pending, "是的")
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.actions[0]["command"], "FOLD")

    def test_final_yes_dispatches_exactly_once(self):
        pending = self.request()
        self.listen(pending)
        for _ in range(4):
            self.answer(pending, "对的。")
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.control.snapshot()["pending"]["state"], "confirmed")

    def test_negative_and_mixed_sentence_cancel(self):
        for text in ("不是", "不是的", "是的但是不要执行", "取消", "别折叠", "等一下"):
            with self.subTest(text=text):
                self.control.observe(None)
                pending = self.request()
                self.listen(pending)
                self.answer(pending, text)
                self.assertEqual(self.control.snapshot()["pending"]["state"], "rejected")
        self.assertEqual(self.actions, [])

    def test_unknown_does_not_count_as_yes_or_extend_deadline(self):
        pending = self.request()
        self.listen(pending)
        before = self.control.snapshot()["pending"]["deadline"]
        self.answer(pending, "这个是怎么回事")
        self.assertEqual(self.actions, [])
        self.assertEqual(self.control.snapshot()["pending"]["deadline"], before)
        self.assertEqual(self.control.snapshot()["pending"]["state"], "listening")

    def test_timeout_after_playback_never_dispatches(self):
        pending = self.request()
        self.advance(5)
        self.listen(pending)
        self.assertAlmostEqual(self.control.snapshot()["pending"]["deadline"], self.now + 8)
        self.advance(8.1)
        self.answer(pending, "是的")
        self.assertEqual(self.actions, [])
        self.assertEqual(self.control.snapshot()["pending"]["state"], "expired")

    def test_prompt_expiration_cannot_be_revived_by_late_listen(self):
        pending = self.request()
        self.advance(30.1)
        with self.assertRaises(ValueError):
            self.control.listen("phone", pending["id"], pending["playback_token"])
        self.assertEqual(self.actions, [])

    def test_listen_requires_audio_delivery_time_and_matching_token(self):
        pending = self.request()
        with self.assertRaises(ValueError):
            self.control.listen("phone", pending["id"], pending["playback_token"])
        self.control.prompt_audio("phone", pending["id"])
        with self.assertRaises(ValueError):
            self.control.listen("phone", pending["id"], pending["playback_token"])
        self.advance(.7)
        with self.assertRaises(ValueError):
            self.control.listen("phone", pending["id"], "wrong")

    def test_disconnect_and_camera_failure_cancel_pending(self):
        pending = self.request()
        self.listen(pending)
        self.advance(2.1, camera=False)
        self.answer(pending, "是的")
        self.assertEqual(self.actions, [])
        self.assertEqual(self.control.snapshot()["pending"]["state"], "cancelled")
        self.control.observe(None)
        self.control.camera_alive()
        pending = self.request()
        self.listen(pending)
        self.control.disconnect("phone")
        self.answer(pending, "是的")
        self.assertEqual(self.actions, [])

    def test_missing_audio_cancels_even_if_status_polling_continues(self):
        pending = self.request()
        self.listen(pending)
        self.advance(3.1, audio=False)
        self.control.snapshot("phone")
        self.answer(pending, "是的")
        self.assertEqual(self.actions, [])
        self.assertFalse(self.control.enabled)

    def test_held_gesture_only_prompts_once(self):
        pending = self.request()
        for _ in range(5):
            self.assertFalse(self.control.observe("握拳"))
        self.listen(pending)
        self.answer(pending, "是的")
        self.assertFalse(self.control.observe("握拳"))
        self.assertEqual(len(self.actions), 1)

    def test_new_gesture_during_pending_is_not_queued(self):
        pending = self.request()
        self.assertFalse(self.control.observe("手掌展开"))
        self.listen(pending)
        self.answer(pending, "取消")
        self.assertFalse(self.control.observe("手掌展开"))
        self.assertEqual(self.control.snapshot()["pending"]["id"], pending["id"])

    def test_late_previous_answer_cannot_confirm_new_request(self):
        old = self.request()
        self.listen(old)
        self.answer(old, "取消")
        self.control.observe(None)
        new = self.request("手掌展开")
        self.listen(new)
        self.answer(old, "是的")
        self.assertEqual(self.actions, [])
        self.answer(new, "好的")
        self.assertEqual(self.actions[0]["command"], "UNFOLD")
        self.assertEqual(self.actions[0]["action"], "展开")
        self.assertIn("方向盘的展开操作", new["prompt"])

    def test_stop_fold_label_and_alias_use_separate_command(self):
        pending = self.request("食指竖起")
        self.assertEqual(pending["command"], "HALF")
        self.assertIn("半折叠", pending["prompt"])

    def test_dispatch_error_does_not_retry(self):
        self.control.dispatch = Mock(side_effect=OSError("synthetic failure"))
        pending = self.request()
        self.listen(pending)
        self.answer(pending, "是的")
        self.answer(pending, "是的")
        self.control.dispatch.assert_called_once()
        self.assertEqual(self.control.snapshot()["pending"]["state"], "dispatch_failed")
        self.assertIsNone(self.control.snapshot()["last_action"])

    def test_no_phone_voice_not_ready_and_close_cannot_execute(self):
        self.control.enabled = False
        self.assertFalse(self.control.observe("握拳"))
        self.control.ready("phone")
        self.control.observe(None)
        self.control.prompts.status = lambda: {"ready": False, "error": "synthetic"}
        self.assertFalse(self.control.observe("握拳"))
        self.control.prompts.status = lambda: {"ready": True, "error": None}
        self.control.observe(None)
        pending = self.request()
        self.listen(pending)
        self.control.close()
        self.answer(pending, "是的")
        self.assertEqual(self.actions, [])

    def test_other_phone_cannot_listen_or_answer(self):
        pending = self.request()
        self.listen(pending)
        self.control.transcript("old-phone", pending["id"], SimpleNamespace(type="final", text="是的"))
        with self.assertRaises(ValueError):
            self.control.listen("other-phone", pending["id"], pending["playback_token"])
        self.assertEqual(self.actions, [])

    def test_strict_yes_no_vocabulary(self):
        for text in ("是的！", "对的", "嗯嗯", "好的，请执行", "没错", "是的，是的", "对对对", "好好好", "嗯，是的，对的"):
            self.assertEqual(core.classify_answer(text), "yes")
        for text in ("是不是", "不是的", "对，但是取消", "请不要执行", "算了算了", "先放着吧", "保持原样就行", "等一等再说", "是的是的，但是先别动", "不用了不用了"):
            self.assertEqual(core.classify_answer(text), "no")
        for text in ("这个是对的吧", "可不可以", "", None, "你刚才说是的吗"):
            self.assertNotEqual(core.classify_answer(text), "yes")

    def test_repeated_affirmative_sends_once_and_natural_negative_cancels(self):
        pending = self.request()
        self.listen(pending)
        self.answer(pending, "是的，是的")
        self.answer(pending, "是的，是的")
        self.assertEqual(len(self.actions), 1)
        for text in ("算了算了", "先保持原样吧", "是的是的，但是先放着"):
            self.control.observe(None)
            pending = self.request()
            self.listen(pending)
            self.answer(pending, text)
            self.assertEqual(self.control.snapshot()["pending"]["state"], "rejected")
        self.assertEqual(len(self.actions), 1)


class SavedModelTests(unittest.TestCase):
    def test_published_minimal_model_load_is_read_only_and_uses_73_numeric_references(self):
        module = import_module(PACKAGE + ".model")
        draft = ROOT / "assets/output_json/reference_library.json"
        before = draft.read_bytes()
        # A media read is a test failure, including the source videos named by the manifest.
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path.suffix.lower() in (".mp4", ".png", ".jpg", ".jpeg", ".avi"):
                raise AssertionError("不得读取保存的图像或视频")
            return original(path, *args, **kwargs)
        # A clean checkout does not need downloaded weights to run state tests.
        # Use the real numerical library with a small, correctly hashed fixture.
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            network = folder / "fixture.task"
            network.write_bytes(b"synthetic-network-for-manifest-validation")
            record = json.loads((ROOT / "assets/output_json/model.json").read_text())
            record["network"]["sha256"] = hashlib.sha256(network.read_bytes()).hexdigest()
            (folder / "reference_library.json").write_bytes(before)
            manifest = folder / "model.json"
            manifest.write_text(json.dumps(record))
            with patch.object(Path, "open", guarded), patch.object(module, "GESTURE_MODEL", network):
                model = module.SavedGestureModel(manifest)
        self.assertEqual(model.policy.kind, "ok")
        self.assertTrue(model.policy.reference_ready_priority)
        self.assertEqual(sum(len(d["features"]) for d in model.library.demonstrations), 73)
        self.assertEqual(draft.read_bytes(), before)

    def test_model_policy_needs_fresh_ok_for_every_operation(self):
        policy_type = import_module("steering_control.gesture.policy").ModelPolicy
        prediction = import_module("steering_control.gesture.features").Prediction
        import json
        config = json.loads((ROOT / "assets/output_json/model.json").read_text())["work_config"]
        policy = policy_type("ok", config, reference_ready_priority=True)
        def frame(label, now):
            reading = SimpleNamespace(score=1., margin=1., hand_present=True)
            return policy.apply(prediction(label, 0., "corrected", origin="correction"), reading, None, 640, 480, now)[0].label
        self.assertIsNone(frame("握拳", 0))
        self.assertIsNone(frame("OK手势", .1))
        self.assertIsNone(frame("OK手势", .3))
        self.assertIsNone(frame("握拳", .4))
        self.assertEqual(frame("握拳", .7), "握拳")
        policy.gate.cancel()
        self.assertIsNone(frame("握拳", .8))
        self.assertIsNone(frame("握拳", 1.1))
        self.assertIsNone(frame("OK手势", 1.2))
        self.assertIsNone(frame("OK手势", 1.4))
        self.assertIsNone(frame("手掌展开", 1.5))
        self.assertEqual(frame("手掌展开", 1.8), "手掌展开")


if __name__ == "__main__":
    unittest.main()
