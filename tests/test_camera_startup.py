"""Camera deployment checks using mocked processes, never drivers or camera frames."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from steering_control import paths
from steering_control.gesture.camera_startup import prepare_camera
from steering_control.run import build_parser, main


SCRIPT = paths.ROOT / "camera_support/sensing_sg8a/scripts/quick_bring_up_auto.sh"


class CameraStartupTests(unittest.TestCase):
    def arguments(self, **changes):
        values = dict(auto_camera=True, stream=None, camera=4, camera_init_script=SCRIPT)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_fresh_clone_uses_packaged_script_and_local_overrides_take_priority(self):
        with patch.dict(paths._local, {}, clear=True), patch.dict(os.environ, {"STEERING_CAMERA_INIT_SCRIPT": ""}):
            self.assertEqual(build_parser().parse_args([]).camera_init_script, SCRIPT)
        with patch.dict(os.environ, {"STEERING_CAMERA_INIT_SCRIPT": "custom/init.sh"}):
            self.assertEqual(build_parser().parse_args([]).camera_init_script, paths.ROOT / "custom/init.sh")
        self.assertEqual(build_parser().parse_args(["--camera-init-script", str(SCRIPT)]).camera_init_script, SCRIPT)

    def test_stream_other_camera_and_explicit_disable_never_start_vendor_script(self):
        for changes in ({"stream": "http://example.test/video"}, {"camera": 0}, {"auto_camera": False}):
            args = self.arguments(**changes)
            with patch("steering_control.gesture.camera_startup.subprocess.run") as run:
                prepare_camera(args)
            run.assert_not_called()
            self.assertIsNone(args.camera_init_error)

    def test_read_only_check_must_succeed_before_initialization(self):
        args = self.arguments()
        result = subprocess.CompletedProcess([], 1, "wrong kernel\n", "")
        with patch("steering_control.gesture.camera_startup.subprocess.run", return_value=result) as run, \
             contextlib.redirect_stderr(io.StringIO()) as output:
            prepare_camera(args)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ["--check", "--no-preview"])
        self.assertIn("wrong kernel", output.getvalue())
        self.assertIn("README.md", args.camera_init_error)

    def test_success_runs_no_preview_and_checks_device_permissions(self):
        args = self.arguments()
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch("steering_control.gesture.camera_startup.subprocess.run", return_value=result) as run, \
             patch.object(Path, "is_char_device", return_value=True), \
             patch("steering_control.gesture.camera_startup.os.access", return_value=True), \
             contextlib.redirect_stderr(io.StringIO()):
            prepare_camera(args)
        self.assertIsNone(args.camera_init_error)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["/bin/bash", str(SCRIPT), "--no-preview"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_missing_script_timeout_and_inaccessible_device_are_reported(self):
        with patch.object(Path, "is_file", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            args = self.arguments()
            prepare_camera(args)
            self.assertIn("不存在", args.camera_init_error)
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch("steering_control.gesture.camera_startup.subprocess.run", return_value=result), \
             patch.object(Path, "is_char_device", return_value=True), \
             patch("steering_control.gesture.camera_startup.os.access", return_value=False), \
             contextlib.redirect_stderr(io.StringIO()):
            args = self.arguments()
            prepare_camera(args)
            self.assertIn("读写权限", args.camera_init_error)
        with patch("steering_control.gesture.camera_startup.subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 15)), \
             contextlib.redirect_stderr(io.StringIO()):
            args = self.arguments()
            prepare_camera(args)
            self.assertIn("timed out", args.camera_init_error)

    def test_failed_initialization_stops_before_starting_speech_and_chat(self):
        def fail(args):
            args.camera_init_error = "synthetic camera failure"
        with patch("steering_control.run.model_module.SavedGestureModel"), \
             patch("steering_control.tts.speech.default_model", return_value=Path("synthetic")), \
             patch("steering_control.gesture.camera_startup.prepare_camera", side_effect=fail), \
             patch("steering_control.run.speech_run.create_recognizer") as speech, \
             patch("steering_control.run.deployment.local_model") as chat, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([]), 2)
        speech.assert_not_called()
        chat.assert_not_called()
