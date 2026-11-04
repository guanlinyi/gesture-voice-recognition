"""Memory-only phone + routing + dialogue + binary speech integration."""
from importlib import import_module
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

PACKAGE = "steering_control"
ROOT = Path(__file__).resolve().parents[1]
legacy = import_module("tests.helpers")
phone = import_module(PACKAGE + ".phone")
core = import_module(PACKAGE + ".confirmation")
interaction = import_module(PACKAGE + ".interaction")


class HTTPChatTests(unittest.TestCase):
    def setUp(self):
        class Client:
            model = "synthetic"
            def stream(self, messages, cancelled):
                yield "你好，继续聊。"
        class Intent(Client):
            def stream(self, messages, cancelled):
                yield '{"kind":"chat","command":"NONE"}'
        self.control = core.Confirmation(lambda action: self.fail("聊天不应发送动作"), model_name="synthetic",
                                         prompts=SimpleNamespace(status=lambda: {"ready": True}))
        self.dialogue = interaction.SteeringDialogue(Client(), control=self.control,
            speech=SimpleNamespace(synthesize=lambda text, cancelled: b"RIFF-synthetic-speech"))
        self.router = interaction.VoiceRouter(self.control, Intent(), self.dialogue)
        self.control.unmatched_handler = self.router.submit
        self.recognizer = legacy.FakeRecognizer()
        self.sessions = phone.ConfirmationSessions(self.recognizer, self.control, router=self.router)
        self.sid = self.sessions.start(48000)["session_id"]
        self.control.ready(self.sid)

    def tearDown(self):
        self.control.close()
        self.router.close()
        self.dialogue.close()

    def request(self, path, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        method = "GET" if body is None else "POST"
        wire = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
        connection = legacy.MemoryConnection(wire)
        phone.handler_for(self.sessions, Path("unused-ca.crt"))(connection, ("127.0.0.1", 1), object())
        return bytes(connection.output).split(b"\r\n\r\n", 1)

    def test_chat_components_and_bound_dialogue_are_available(self):
        for path in ("/reply.js", "/chat.js"):
            self.assertIn(b"200 OK", self.request(path)[0])
        _, body = self.request(f"/api/confirmation/status?session={self.sid}")
        state = json.loads(body)
        self.assertEqual(state["chat"]["dialogue"]["dialogue_id"], self.router.dialogue_id)

    def test_spoken_chat_flows_to_streaming_answer_and_binary_tts_endpoint(self):
        import numpy as np
        token = self.control.snapshot()["input"]["token"]
        self.recognizer.text, self.recognizer.endpoint = "你好你叫什么名字", True
        self.sessions.audio_for_request(self.sid, 0, np.zeros(4800, dtype="<f4").tobytes(), token)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            turns = self.router.snapshot()["dialogue"]["turns"]
            if turns and turns[0]["state"] == "done":
                break
            time.sleep(.005)
        self.assertEqual(turns[0]["assistant"], "你好，继续聊。")
        headers, body = self.request("/api/dialogue/speech", {"dialogue_id": self.router.dialogue_id,
            "turn_id": turns[0]["id"], "start": 0, "end": 2})
        self.assertIn(b"audio/wav", headers)
        self.assertEqual(body, b"RIFF-synthetic-speech")
        self.sessions.stop(self.sid)
        headers, _ = self.request(f"/api/confirmation/status?session={self.sid}")
        self.assertIn(b"400", headers)
