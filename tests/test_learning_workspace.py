"""Workspace callbacks and single-camera loop, without a desktop or saved media."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

import numpy as np

from steering_control.confirmation import Confirmation
from steering_control.learning.panel import LearningPanel
from steering_control.preview import Preview
from steering_control.run import live_loop


def control():
    result = Confirmation(Mock(), model_name="synthetic", prompts=SimpleNamespace(status=lambda: {"ready": True}), clock=lambda: 100.)
    result.connect("phone")
    result.ready("phone")
    result.camera_alive()
    return result


class WorkspaceTests(unittest.TestCase):
    def test_switch_pauses_commands_cancels_gate_and_return_requires_fresh_token(self):
        window = Preview.__new__(Preview)
        window.control = control()
        window.pages = Mock()
        window.pages.index.return_value = 1
        window.mode = "recognition"
        window.active_model = SimpleNamespace(policy=SimpleNamespace(gate=Mock()))
        window.learning = SimpleNamespace(busy=False)
        window.learning_panel = SimpleNamespace(action=lambda callback: callback(), stop_recording=Mock())
        window.on_pause = Mock()
        old = window.control.snapshot()["input"]["token"]
        window.mode_changed()
        self.assertTrue(window.control.learning)
        self.assertEqual(window.control.snapshot()["input"]["mode"], "muted")
        window.active_model.policy.gate.cancel.assert_called_once()
        window.pages.index.return_value = 0
        window.mode_changed()
        self.assertFalse(window.control.learning)
        window.learning_panel.stop_recording.assert_called_once()
        self.assertNotEqual(window.control.snapshot()["input"]["token"], old)
        self.assertEqual(window.on_pause.call_count, 2)

    def test_learning_job_keeps_page_paused_until_completion(self):
        window = Preview.__new__(Preview)
        window.pages = Mock()
        window.pages.index.return_value = 0
        window.mode = "learning"
        window.learning = SimpleNamespace(busy=True)
        window.learning_panel = Mock()
        window.control = control()
        window.control.set_learning(True)
        window.mode_changed()
        self.assertTrue(window.control.learning)
        window.pages.select.assert_called_with(window.learning_panel)

    def test_saved_photograph_and_video_select_the_corresponding_tab(self):
        panel = LearningPanel.__new__(LearningPanel)
        panel.session = Mock()
        panel.session.photograph.return_value = {"id": "photo"}
        panel.session.stop_video.return_value = {"id": "video"}
        panel.trees = {"image": Mock(), "video": Mock()}
        panel.label = Mock()
        panel.label.get.return_value = "握拳"
        panel.tabs, panel.label_box, panel.refresh = Mock(), Mock(), Mock()
        panel.photo()
        panel.tabs.select.assert_called_with(0)
        panel.session.recording = {"split": "reference"}
        panel.video_toggle("reference")
        panel.tabs.select.assert_called_with(1)
        panel.session.stop_video.assert_called_once()
        panel.session.recording = {"split": "validation"}
        with self.assertRaises(ValueError):
            panel.video_toggle("reference")

    def test_typing_q_in_a_model_name_does_not_close_the_window(self):
        window = Preview.__new__(Preview)
        window.root, window.close = Mock(), Mock()
        window.root.focus_get.return_value.winfo_class.return_value = "TEntry"
        window.quit_shortcut()
        window.close.assert_not_called()
        window.root.focus_get.return_value.winfo_class.return_value = "TLabel"
        window.quit_shortcut()
        window.close.assert_called_once()

    def test_two_pages_share_one_capture_and_collect_before_display_drawing(self):
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        source = Mock()
        source.read.return_value = (True, frame)
        reading = SimpleNamespace(world_landmarks=None)
        prediction = SimpleNamespace(label="握拳")
        model = SimpleNamespace(model_path="synthetic.task", classifier_options={}, predict=Mock(return_value=prediction),
                                policy=SimpleNamespace(gate=Mock(phase="ready"), apply=Mock()))
        current = control()
        window = Mock()
        window.active_model = model
        window.is_learning = True
        window.learning.validation_model = None
        window.update.return_value = True
        current.set_learning(True)
        args = SimpleNamespace(stream=None, camera=4, no_show=False, window_size="1600x900", max_frames=3,
                               max_width=640, draw_landmarks=False)
        recognizer = Mock()
        recognizer.read.return_value = (reading, None)
        with patch("steering_control.hardware.capture.open_source", return_value=source) as opening, \
             patch("steering_control.hardware.gestures.classifier.GestureClassifier") as factory, \
             patch("steering_control.preview.Preview", return_value=window):
            factory.return_value.__enter__.return_value = recognizer
            live_loop(args, model, current)
        opening.assert_called_once_with(stream=None, camera=4)
        self.assertEqual(source.read.call_count, 3)
        self.assertEqual(window.learning.on_frame.call_count, 3)
        model.policy.apply.assert_not_called()
        current.dispatch.assert_not_called()
        source.release.assert_called_once()
        window.destroy.assert_called_once()
