"""In-memory HTTP/ASR tests. No listening sockets or real microphone."""
from importlib import import_module
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ASR = "steering_control.asr"
legacy = import_module("tests.helpers")
PACKAGE = "steering_control"
phone = import_module(PACKAGE + ".phone")
core = import_module(PACKAGE + ".confirmation")


class Prompts:
    def status(self):
        return {"ready": True, "error": None}
    def get(self, label):
        return b"RIFFsynthetic", .1


class ConfirmationPhoneTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.actions = []
        self.control = core.Confirmation(self.actions.append, model_name="synthetic", prompts=Prompts(), clock=lambda: self.now)
        self.recognizer = legacy.FakeRecognizer()
        self.sessions = phone.ConfirmationSessions(self.recognizer, self.control, clock=lambda: self.now)
        self.sid = self.sessions.start(48000)["session_id"]
        self.control.ready(self.sid)
        self.control.camera_alive()
        self.control.observe("握拳")
        self.pending = self.control.snapshot()["pending"]
        self.audio = np.ones(4800, dtype="<f4").tobytes()

    def request(self, path, body=None):
        data = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        method = "GET" if body is None else "POST"
        wire = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
        connection = legacy.MemoryConnection(wire)
        phone.handler_for(self.sessions, Path("unused-ca.crt"))(connection, ("127.0.0.1", 1), object())
        return bytes(connection.output).split(b"\r\n\r\n", 1)

    def listen(self):
        self.control.prompt_audio(self.sid, self.pending["id"])
        self.now += .6
        self.control.camera_alive()
        self.sessions.listen(self.sid, self.pending["id"], self.pending["playback_token"])

    def test_page_is_dedicated_to_confirmation_and_reuses_capture_js(self):
        headers, body = self.request("/")
        self.assertIn(b"200 OK", headers)
        self.assertIn(b'/confirmation.js', body)
        self.assertNotIn(b'/dialogue.js', body)
        self.assertIn(b"captureHooks", self.request("/phone.js")[1])

    def test_prompt_is_binary_and_no_unbound_status_access(self):
        headers, body = self.request(f"/api/confirmation/prompt?session={self.sid}&request={self.pending['id']}")
        self.assertIn(b"audio/wav", headers)
        self.assertEqual(body, b"RIFFsynthetic")
        self.assertIn(b"400", self.request("/api/confirmation/status")[0])

    def test_early_or_wrong_request_listen_rejected(self):
        for request_id in (self.pending["id"], "wrong"):
            headers, body = self.request("/api/confirmation/listen", {"session_id": self.sid,
                "request_id": request_id, "playback_token": self.pending["playback_token"]})
            self.assertIn(b"400", headers)
        self.assertEqual(self.actions, [])

    def test_only_final_tagged_confirmation_dispatches(self):
        self.listen()
        self.recognizer.text = "是的"
        url = f"/api/audio?session={self.sid}&sequence=0&tag={self.pending['id']}"
        headers, _ = self.request(url, self.audio)
        self.assertIn(b"200 OK", headers)
        self.assertEqual(self.actions, [])
        self.recognizer.endpoint = True
        self.request(url.replace("sequence=0", "sequence=1"), self.audio)
        self.assertEqual(len(self.actions), 1)
        self.request(url.replace("sequence=0", "sequence=2"), self.audio)
        self.assertEqual(len(self.actions), 1)
        history = self.control.snapshot()["history"]
        self.assertTrue(any(row["role"] == "user" and row["text"] == "是的" and row["state"] == "done" for row in history))
        self.assertTrue(any(row["kind"] == "sent" and "数字 1" in row["text"] for row in history))

    def test_stale_tag_muted_and_yes_does_not_execute(self):
        self.listen()
        self.recognizer.text, self.recognizer.endpoint = "是的", True
        self.sessions.audio_for_request(self.sid, 0, self.audio, "previous-request")
        self.assertEqual(self.actions, [])
        self.assertTrue(np.all(self.recognizer.stream.received[-1][1] == 0))

    def test_prompt_phase_audio_muted_and_stop_does_not_flush_yes(self):
        self.recognizer.text = "是的"
        self.sessions.audio_for_request(self.sid, 0, self.audio, self.pending["id"])
        self.assertTrue(np.all(self.recognizer.stream.received[-1][1] == 0))
        self.listen()
        self.recognizer.text = "是的"
        self.sessions.stop(self.sid)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.control.snapshot()["pending"]["state"], "cancelled")

    def test_listen_resets_recognition_and_cancel_disables_request(self):
        before = self.sessions.active["asr"]
        self.listen()
        self.assertIsNot(before, self.sessions.active["asr"])
        headers, body = self.request("/api/confirmation/cancel", {"session_id": self.sid, "request_id": self.pending["id"]})
        self.assertIn(b"200 OK", headers)
        self.recognizer.text, self.recognizer.endpoint = "是的", True
        self.sessions.audio_for_request(self.sid, 0, self.audio, self.pending["id"])
        self.assertEqual(self.actions, [])

    def test_audio_sequence_still_rejects_duplicates(self):
        self.sessions.audio_for_request(self.sid, 0, self.audio, None)
        with self.assertRaises(ValueError):
            self.sessions.audio_for_request(self.sid, 0, self.audio, None)

    def test_each_gesture_routes_its_own_question_and_prompt_audio(self):
        self.control.prompts.get = lambda label: (b"RIFF" + core.ACTIONS[label].prompt.encode(), .1)
        for label, action in core.ACTIONS.items():
            with self.subTest(label=label):
                self.control.cancel(self.sid, self.control.snapshot()["pending"]["id"])
                self.control.observe(None)
                self.assertTrue(self.control.observe(label))
                current = self.control.snapshot()["pending"]
                headers, body = self.request(f"/api/confirmation/status?session={self.sid}")
                self.assertIn(b"200 OK", headers)
                self.assertEqual(json.loads(body)["pending"]["prompt"], action.prompt)
                headers, body = self.request(f"/api/confirmation/prompt?session={self.sid}&request={current['id']}")
                self.assertIn(b"audio/wav", headers)
                self.assertEqual(body, b"RIFF" + action.prompt.encode())

    def test_continuous_phone_audio_accepts_three_direct_commands_without_gestures(self):
        self.control.cancel(self.sid, self.pending["id"])
        for sequence, (text, number) in enumerate((("折叠方向盘", 1), ("半折叠方向盘", 2), ("展开方向盘", 3))):
            token = self.control.snapshot()["input"]["token"]
            self.recognizer.text, self.recognizer.endpoint = text, True
            result = self.sessions.audio_for_request(self.sid, sequence, self.audio, token)
            self.assertEqual(result["events"][0]["type"], "final")
            self.assertEqual(self.actions[-1]["id"], number)
            self.control.update_arm_state(self.actions[-1]["action"], moving=False)
        self.assertEqual([event["id"] for event in self.actions], [1, 2, 3])

    def test_pausing_phone_disables_direct_voice_before_tail_flush(self):
        self.control.cancel(self.sid, self.pending["id"])
        token = self.control.snapshot()["input"]["token"]
        headers, body = self.request("/api/confirmation/pause", {"session_id": self.sid})
        self.assertIn(b"200 OK", headers)
        self.assertEqual(json.loads(body)["input"]["mode"], "muted")
        self.recognizer.text, self.recognizer.endpoint = "折叠方向盘", True
        self.sessions.audio_for_request(self.sid, 0, self.audio, token)
        self.assertEqual(self.actions, [])


if __name__ == "__main__":
    unittest.main()
