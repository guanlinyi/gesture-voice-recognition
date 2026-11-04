"""Motion interlocks use feedback, never an elapsed-time guess or a model reply."""
from importlib import import_module
from types import SimpleNamespace
import unittest

P = "steering_control"
core = import_module(P + ".confirmation")
view = import_module(P + ".presentation")
commands = import_module(P + ".commands")


class ArmStateTests(unittest.TestCase):
    def setUp(self):
        self.now, self.actions = 100., []
        self.control = core.Confirmation(self.actions.append, model_name="synthetic", clock=lambda: self.now,
            prompts=SimpleNamespace(status=lambda: {"ready": True}, get=lambda label: (b"wav", .1)))
        self.control.connect("phone")
        self.control.ready("phone")
        self.control.camera_alive()

    def say(self, text):
        token = self.control.snapshot()["input"]["token"]
        self.control.direct_transcript("phone", token, SimpleNamespace(type="final", text=text))

    def test_folding_rejects_unfolding_and_keeps_current_state(self):
        self.say("折叠方向盘")
        self.say("展开方向盘")
        state = self.control.snapshot()
        self.assertEqual([action["id"] for action in self.actions], [1])
        self.assertEqual(state["arm_state"]["state"], "折叠")
        self.assertEqual(state["pending"]["state"], "rejected_state")
        self.assertEqual(state["last_rejection"]["text"], "当前状态：折叠，无法执行展开命令")
        notice = view.SentCommandNotice()
        self.assertEqual(notice.update(state["last_action"], state["last_rejection"]), "当前状态：折叠，无法执行展开命令")
        self.assertIn("当前状态：折叠", view.arm_caption(state))

    def test_unfolding_rejects_folding_including_model_interpretation(self):
        self.say("展开方向盘")
        token = self.control.snapshot()["input"]["token"]
        self.assertFalse(self.control.submit_voice_intent("phone", token, "FOLD", "帮我把它收起来"))
        self.assertEqual([action["id"] for action in self.actions], [3])
        self.assertEqual(self.control.snapshot()["last_rejection"]["text"], "当前状态：展开，无法执行折叠命令")

    def test_feedback_completion_releases_interlock_not_elapsed_time(self):
        self.say("折叠方向盘")
        self.now += 1000
        self.control.heartbeat("phone")
        self.say("展开方向盘")
        self.assertEqual(len(self.actions), 1)
        self.control.update_arm_state("折叠", moving=False)
        self.say("展开方向盘")
        self.assertEqual([action["id"] for action in self.actions], [1, 3])

    def test_half_fold_and_half_unfold_share_id_and_feedback_state(self):
        for phrase in ("方向盘半折叠", "方向盘半展开", "帮我把方向盘半展开"):
            self.assertEqual(commands.parse_voice_command(phrase), "HALF")
            self.say(phrase)
            self.assertEqual((self.actions[-1]["id"], self.actions[-1]["command"], self.actions[-1]["action"]), (2, "HALF", "半折叠"))
        self.control.update_arm_state("半展开", moving=False)
        self.assertEqual(self.control.snapshot()["arm_state"]["state"], "半折叠")

    def test_opposite_gesture_is_rejected_before_question(self):
        self.control.update_arm_state("FOLD", moving=True)
        self.assertFalse(self.control.observe("手掌展开"))
        self.assertIsNone(self.control.snapshot()["pending"])
        self.assertEqual(self.actions, [])

    def test_state_is_checked_again_after_user_confirms_gesture(self):
        self.assertTrue(self.control.observe("手掌展开"))
        pending = self.control.snapshot()["pending"]
        self.control.prompt_audio("phone", pending["id"])
        self.now += .2
        self.control.listen("phone", pending["id"], pending["playback_token"])
        self.control.update_arm_state("折叠", moving=True)
        self.control.transcript("phone", pending["id"], SimpleNamespace(type="final", text="是的"))
        self.assertEqual(self.actions, [])
        self.assertEqual(self.control.snapshot()["pending"]["state"], "rejected_state")

    def test_failed_dispatch_restores_state_and_preserves_newer_feedback(self):
        def fail(action):
            raise OSError("synthetic")
        self.control.dispatch = fail
        self.say("折叠方向盘")
        self.assertEqual(self.control.snapshot()["arm_state"]["state"], "未知")
        def feedback_then_fail(action):
            self.control.update_arm_state("半折叠", moving=False)
            raise OSError("synthetic")
        self.control.dispatch = feedback_then_fail
        self.say("折叠方向盘")
        self.assertEqual(self.control.snapshot()["arm_state"]["state"], "半折叠")

    def test_initial_unknown_and_completed_status_are_explicit(self):
        self.assertEqual(view.arm_caption(self.control.snapshot()), "当前状态：未知")
        self.control.update_arm_state("展开", moving=False)
        self.assertEqual(view.arm_caption(self.control.snapshot()), "当前状态：展开（已完成）")

    def test_half_target_does_not_erase_an_inflight_direction(self):
        self.say("折叠方向盘")
        self.say("半展开方向盘")
        self.say("展开方向盘")
        self.assertEqual([action["id"] for action in self.actions], [1, 2])
        self.assertEqual(self.control.snapshot()["arm_state"]["state"], "折叠")
        self.control.update_arm_state("半展开", moving=False)
        self.say("展开方向盘")
        self.assertEqual([action["id"] for action in self.actions], [1, 2, 3])
