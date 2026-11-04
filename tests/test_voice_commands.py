"""Direct voice and confirmed gestures share IDs, output and successful-send notice."""
from contextlib import redirect_stdout
from importlib import import_module
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PACKAGE = "steering_control"
core = import_module(PACKAGE + ".confirmation")
commands = import_module(PACKAGE + ".commands")
view = import_module(PACKAGE + ".presentation")


class VoiceCommandTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.control = core.Confirmation(self.actions.append, model_name="synthetic",
                                         prompts=SimpleNamespace(status=lambda: {"ready": True, "error": None},
                                                                 get=lambda label: (b"wav", .1)), clock=lambda: 100.)
        self.control.connect("phone")
        self.control.ready("phone")

    def say(self, text, kind="final", token=None):
        token = token or self.control.snapshot()["input"]["token"]
        self.control.direct_transcript("phone", token, SimpleNamespace(type=kind, text=text))

    def test_direct_commands_work_without_ok_or_hand_and_produce_123(self):
        for text, number, action in (("将方向盘进行折叠", 1, "折叠"), ("半折叠方向盘", 2, "半折叠"),
                                     ("展开方向盘", 3, "展开")):
            self.say(text)
            sent = self.control.snapshot()["last_action"]
            self.assertEqual((sent["id"], sent["action"], sent["source"]), (number, action, "voice"))
            self.assertEqual(view.SentCommandNotice().update(sent), f"发送方向盘的{action}命令")
            self.control.update_arm_state(action, moving=False)
        self.assertEqual([action["id"] for action in self.actions], [1, 2, 3])
        self.assertIsNone(self.control.camera_at)

    def test_parser_accepts_full_imperatives_and_rejects_negations_questions_and_multi_actions(self):
        for text, command in (("请把方向盘折叠一下", "FOLD"), ("请展开方向盘", "UNFOLD"),
                              ("半展开方向盘", "HALF"), ("将方向盘进行半折叠", "HALF"),
                              ("帮我将方向盘进行展开操作", "UNFOLD")):
            self.assertEqual(commands.parse_voice_command(text), command, text)
        for text in ("不要折叠方向盘", "别展开方向盘", "取消半折叠方向盘", "折叠方向盘吗", "展开方向盘？",
                     "你知道怎么折叠方向盘吗", "如果需要就折叠方向盘", "先折叠方向盘再展开方向盘", "是的", "折叠", "停止折叠方向盘", "暂停方向盘的折叠"):
            self.assertIsNone(commands.parse_voice_command(text), text)

    def test_partial_yes_and_stale_token_cannot_submit_or_repeat(self):
        old = self.control.snapshot()["input"]["token"]
        self.say("折叠方向盘", "partial")
        self.say("是的")
        self.assertEqual(self.actions, [])
        self.say("折叠方向盘", token=old)
        self.say("折叠方向盘", token=old)
        self.assertEqual(len(self.actions), 1)
        self.say("折叠方向盘")
        self.assertEqual(len(self.actions), 2)

    def test_gesture_confirmation_blocks_direct_command_until_finished(self):
        token = self.control.snapshot()["input"]["token"]
        self.control.camera_alive()
        self.assertTrue(self.control.observe("握拳"))
        self.say("展开方向盘", token=token)
        self.assertEqual(self.actions, [])
        pending = self.control.snapshot()["pending"]
        self.assertEqual(self.control.snapshot()["input"]["mode"], "muted")
        self.control.cancel("phone", pending["id"])
        self.assertEqual(self.control.snapshot()["input"]["mode"], "commands")
        self.say("展开方向盘", token=token)
        self.assertEqual(self.actions, [])
        self.say("展开方向盘")
        self.assertEqual(self.actions[0]["id"], 3)

    def test_pause_and_disconnect_disable_direct_commands(self):
        old = self.control.snapshot()["input"]["token"]
        self.control.pause("phone")
        self.say("折叠方向盘", token=old)
        self.assertEqual(self.actions, [])
        self.control.ready("phone")
        token = self.control.snapshot()["input"]["token"]
        self.control.disconnect("phone")
        self.say("展开方向盘", token=token)
        self.assertEqual(self.actions, [])

    def test_direct_dispatch_failure_does_not_show_success_or_retry(self):
        def fail(event):
            raise OSError("synthetic failure")
        self.control.dispatch = fail
        old = self.control.snapshot()["input"]["token"]
        self.say("折叠方向盘", token=old)
        self.assertIsNone(self.control.snapshot()["last_action"])
        self.assertEqual(self.control.snapshot()["pending"]["state"], "dispatch_failed")
        self.say("折叠方向盘", token=old)
        self.assertEqual(self.actions, [])


class NumericOutputTests(unittest.TestCase):
    def test_full_gesture_confirmation_path_prints_each_corresponding_number(self):
        module = import_module(PACKAGE + ".run")
        log, stdout, now = io.StringIO(), io.StringIO(), [100.]
        with patch.object(Path, "open", return_value=log):
            output = module.Output(Path("unused"))
        control = core.Confirmation(output.dispatch, model_name="synthetic",
                                    prompts=SimpleNamespace(status=lambda: {"ready": True}, get=lambda label: (b"wav", .1)),
                                    clock=lambda: now[0])
        control.connect("phone")
        control.ready("phone")
        with redirect_stdout(stdout):
            for label in ("握拳", "摆1字", "手掌展开"):
                control.camera_alive()
                control.observe(None)
                self.assertTrue(control.observe(label))
                pending = control.snapshot()["pending"]
                control.prompt_audio("phone", pending["id"])
                now[0] += .6
                control.heartbeat("phone")
                control.camera_alive()
                control.listen("phone", pending["id"], pending["playback_token"])
                control.transcript("phone", pending["id"], SimpleNamespace(type="final", text="是的"))
                control.update_arm_state(control.snapshot()["last_action"]["action"], moving=False)
        self.assertEqual(stdout.getvalue(), "1\n2\n3\n")
        self.assertEqual([json.loads(line)["source"] for line in log.getvalue().splitlines()], ["gesture"] * 3)
        output.close()

    def test_voice_and_confirmed_gestures_print_only_digits_and_keep_json_log(self):
        module = import_module(PACKAGE + ".run")
        for source in ("voice", "gesture"):
            log, stdout = io.StringIO(), io.StringIO()
            with patch.object(Path, "open", return_value=log):
                output = module.Output(Path("unused"))
            with redirect_stdout(stdout):
                for number in (1, 2, 3):
                    output.dispatch({"type": "confirmed_action", "source": source, "id": number})
            self.assertEqual(stdout.getvalue(), "1\n2\n3\n")
            self.assertEqual([json.loads(line)["id"] for line in log.getvalue().splitlines()], [1, 2, 3])
            output.close()

    def test_jsonl_console_format_remains_available(self):
        module = import_module(PACKAGE + ".run")
        log, stdout = io.StringIO(), io.StringIO()
        with patch.object(Path, "open", return_value=log):
            output = module.Output(Path("unused"), format="jsonl")
        with redirect_stdout(stdout):
            output.dispatch({"id": 2, "action": "半折叠"})
        self.assertEqual(json.loads(stdout.getvalue())["id"], 2)
        output.close()
