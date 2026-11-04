"""Fake-model integration: natural commands, dialogue history, cancellation and schema."""
from importlib import import_module
import io
import json
import threading
import time
from types import SimpleNamespace
import unittest

PACKAGE = "steering_control"
module = import_module(PACKAGE + ".interaction")
core = import_module(PACKAGE + ".confirmation")


def wait_for(test):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.005)
    raise AssertionError("模拟处理未完成")


class Chat:
    model = "synthetic-chat"
    def __init__(self):
        self.messages = []
    def stream(self, messages, cancelled):
        self.messages.append(messages)
        yield "你好，"
        yield "可以继续聊。"


class Intent:
    model = "synthetic-intent"
    def __init__(self):
        self.result = {"kind": "chat", "command": "NONE"}
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.messages = []
    def stream(self, messages, cancelled):
        self.messages.append(messages)
        self.entered.set()
        self.release.wait(3)
        yield self.result if isinstance(self.result, str) else json.dumps(self.result)


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.control = core.Confirmation(self.actions.append, model_name="synthetic",
            prompts=SimpleNamespace(status=lambda: {"ready": True}), clock=lambda: 100.)
        self.chat, self.intent = Chat(), Intent()
        self.dialogue = module.SteeringDialogue(self.chat, control=self.control)
        self.router = module.VoiceRouter(self.control, self.intent, self.dialogue)
        self.control.unmatched_handler = self.router.submit
        self.control.partial_handler = self.router.cancel_interpretation
        self.control.connect("phone")
        self.router.connect("phone")
        self.control.ready("phone")

    def tearDown(self):
        self.control.close()
        self.intent.release.set()
        self.router.close()
        self.dialogue.close()

    def say(self, text):
        token = self.control.snapshot()["input"]["token"]
        self.control.direct_transcript("phone", token, SimpleNamespace(type="final", text=text))

    def test_natural_expression_model_result_submits_one_of_three_actions(self):
        self.intent.result = {"kind": "command", "command": "FOLD"}
        self.say("方向盘挡着了，帮我收起来")
        wait_for(lambda: bool(self.actions))
        self.assertEqual((self.actions[0]["id"], self.actions[0]["interpretation"]), (1, "model"))
        self.assertEqual(self.actions[0]["confirmation"], "方向盘挡着了，帮我收起来")
        self.assertEqual(self.router.snapshot()["dialogue"]["turns"], [])

    def test_chat_streams_and_second_question_receives_completed_history(self):
        self.say("你好，今天怎么样")
        wait_for(lambda: self.router.snapshot()["dialogue"]["turns"] and self.router.snapshot()["dialogue"]["turns"][-1]["state"] == "done")
        self.say("刚才你说了什么")
        wait_for(lambda: len(self.chat.messages) == 2)
        self.assertEqual(self.chat.messages[-1][-3]["content"], "你好，今天怎么样")
        self.assertEqual(self.chat.messages[-1][-2]["content"], "你好，可以继续聊。")
        self.assertEqual(self.actions, [])

    def test_question_and_ambiguous_reply_never_execute(self):
        for kind, text in (("chat", "方向盘怎么折叠"), ("clarify", "把那个弄一下")):
            self.intent.result = {"kind": kind, "command": "NONE"}
            before = len(self.intent.messages)
            self.say(text)
            wait_for(lambda: len(self.intent.messages) > before and self.router.snapshot()["phase"] != "understanding")
        self.assertEqual(self.actions, [])

    def test_bad_json_or_unlisted_action_fails_closed(self):
        for output in ('不是 JSON', '{"kind":"command","command":"DELETE"}', '{"kind":"chat","command":"FOLD"}'):
            self.intent.result = output
            self.say("随便说一点什么")
            wait_for(lambda: self.router.snapshot()["phase"] == "error")
            self.assertEqual(self.actions, [])

    def test_late_model_result_cannot_execute_after_new_gesture(self):
        self.intent.release.clear()
        self.intent.result = {"kind": "command", "command": "UNFOLD"}
        self.say("帮我把它打开")
        self.assertTrue(self.intent.entered.wait(1))
        self.control.camera_alive()
        self.assertTrue(self.control.observe("握拳"))
        self.intent.release.set()
        wait_for(lambda: self.router.jobs.unfinished_tasks == 0)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.router.snapshot()["phase"], "idle")

    def test_late_model_result_cannot_execute_after_stop(self):
        self.intent.release.clear()
        self.intent.result = {"kind": "command", "command": "FOLD"}
        self.say("帮我把它收起来")
        self.assertTrue(self.intent.entered.wait(1))
        self.control.pause("phone")
        self.router.stop_reply()
        self.intent.release.set()
        wait_for(lambda: self.router.jobs.unfinished_tasks == 0)
        self.assertEqual(self.actions, [])

    def test_new_question_invalidates_older_interpretation(self):
        self.intent.release.clear()
        self.say("帮我弄一下方向盘")
        self.assertTrue(self.intent.entered.wait(1))
        self.say("先聊天，你叫什么名字")
        self.intent.release.set()
        wait_for(lambda: self.router.jobs.unfinished_tasks == 0)
        turns = self.router.snapshot()["dialogue"]["turns"]
        self.assertEqual([turn["user"] for turn in turns], ["先聊天，你叫什么名字"])
        self.assertEqual(self.actions, [])

    def test_explicit_command_bypasses_model_and_chat_context_knows_only_submitted_state(self):
        self.say("帮我把方向盘折叠")
        self.assertEqual(self.actions[0]["id"], 1)
        self.assertEqual(self.intent.messages, [])
        self.say("刚才发出了什么指令")
        wait_for(lambda: bool(self.chat.messages))
        self.assertIn("折叠", self.chat.messages[-1][0]["content"])
        self.assertIn("不代表实物完成", self.chat.messages[-1][0]["content"])

    def test_starting_to_speak_again_cancels_old_interpretation_before_a_final_result(self):
        self.intent.release.clear()
        self.intent.result = {"kind": "command", "command": "FOLD"}
        self.say("把方向盘收起来吧")
        self.assertTrue(self.intent.entered.wait(1))
        token = self.control.snapshot()["input"]["token"]
        self.control.direct_transcript("phone", token, SimpleNamespace(type="partial", text="等一下"))
        self.intent.release.set()
        wait_for(lambda: self.router.jobs.unfinished_tasks == 0)
        self.assertEqual(self.actions, [])


class IntentClientTests(unittest.TestCase):
    def test_schema_parameters_are_sent_to_existing_model_api(self):
        requests = []
        def opener(request, timeout):
            requests.append(json.loads(request.data))
            packet = {"choices": [{"delta": {"content": '{"kind":"chat","command":"NONE"}'}, "finish_reason": "stop"}]}
            return io.BytesIO(b"data: " + json.dumps(packet).encode() + b"\n\ndata: [DONE]\n\n")
        schema = {"type": "json_schema", "json_schema": {"name": "steering_intent", "strict": True, "schema": module.INTENT_SCHEMA}}
        client = module.dialogue_module.ChatClient(opener=opener, max_tokens=96, temperature=0., response_format=schema)
        text = "".join(client.stream([{"role": "user", "content": "你好"}], threading.Event()))
        self.assertEqual(module.parse_intent(text), ("chat", "NONE"))
        self.assertEqual(requests[0]["response_format"]["json_schema"]["schema"], module.INTENT_SCHEMA)
        self.assertEqual((requests[0]["max_tokens"], requests[0]["temperature"]), (96, 0.))
