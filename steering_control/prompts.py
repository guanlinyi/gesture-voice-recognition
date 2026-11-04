"""Precompute the three fixed questions once; no recordings or audio files."""
import io
import sys
import threading
import wave

from .confirmation import ACTIONS


class PromptCache:
    def __init__(self, speech):
        self.speech = speech
        self.lock = threading.Lock()
        self.audio = {}
        self.error = None
        self.cancelled = threading.Event()
        self.thread = None

    def status(self):
        with self.lock:
            return {"ready": len(self.audio) == len(ACTIONS), "error": self.error}

    def get(self, label):
        with self.lock:
            if label not in self.audio:
                raise ValueError("确认提示音尚未准备好")
            return self.audio[label]

    def start(self):
        def prepare():
            try:
                for label, action in ACTIONS.items():
                    if self.cancelled.is_set():
                        return
                    data = self.speech.synthesize(action.prompt, self.cancelled)
                    with wave.open(io.BytesIO(data), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                    with self.lock:
                        self.audio[label] = (data, duration)
                print("三种语音确认提示音已就绪（内存缓存）", file=sys.stderr, flush=True)
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                print(f"确认提示音准备失败：{exc}", file=sys.stderr, flush=True)
        self.thread = threading.Thread(target=prepare, daemon=True, name="confirmation-prompts")
        self.thread.start()

    def close(self):
        self.cancelled.set()
