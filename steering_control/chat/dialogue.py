"""Background multi-turn text dialogue. No model/network activity on import."""
from __future__ import annotations

import copy
import json
import queue
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .http_api import api_urlopen

DEFAULT_API = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "local-voice-chat"
SYSTEM_PROMPT = (
    "你是本地中文语音助手。自然、简洁地回答，通常先用两三句话说清重点，用户需要时再详细解释。"
    "结合之前的对话理解追问。输入可能来自语音转写，不清楚时询问。"
    "仅进行对话，不声称已操作未接入的设备或执行未提供的工具。"
)


class ChatClient:
    def __init__(self, base_url=DEFAULT_API, model=DEFAULT_MODEL, timeout=90, opener=None,
                 max_tokens=384, temperature=0.7, response_format=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("对话地址需要是 http(s)://主机:端口/v1")
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.opener = opener if opener is not None else api_urlopen
        self.max_tokens, self.temperature, self.response_format = max_tokens, temperature, response_format

    def stream(self, messages, cancelled):
        if cancelled.is_set():
            return
        payload = {"model": self.model, "messages": messages, "stream": True,
                   "max_tokens": self.max_tokens, "temperature": self.temperature, "top_p": 0.8,
                   "chat_template_kwargs": {"enable_thinking": False}}
        if self.response_format is not None:
            payload["response_format"] = self.response_format
        request = Request(self.base_url + "/chat/completions", json.dumps(payload).encode("utf-8"),
                          {"Content-Type": "application/json", "Accept": "text/event-stream"})
        completed = False
        try:
            with self.opener(request, timeout=self.timeout) as response:
                while not cancelled.is_set():
                    line = response.readline(1024 * 1024)
                    if not line:
                        break
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        completed = True
                        break
                    packet = json.loads(data)
                    if "error" in packet:
                        raise RuntimeError("对话服务返回错误，请查看本地模型日志")
                    choices = packet.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    text = choice.get("delta", {}).get("content")
                    if isinstance(text, str) and text and not cancelled.is_set():
                        yield text
                    if choice.get("finish_reason") is not None:
                        completed = True
                if not cancelled.is_set() and not completed:
                    raise RuntimeError("回答连接提前中断，这次回答未完成")
        except HTTPError as exc:
            raise RuntimeError(f"对话服务返回 HTTP {exc.code}，请查看模型服务日志") from exc
        except (URLError, OSError) as exc:
            raise RuntimeError("无法连接本地对话模型；请运行 python run.py 启动，或检查 --chat-api") from exc


class Dialogue:
    """Queue inference independently of ASR, retaining only completed turn pairs.

    A single worker serializes model requests; the audio thread only enqueues.
    Cancellation/reset invalidate jobs immediately. Cancellation reaches the
    backend when its next response chunk is read (not acoustic barge-in).
    """
    def __init__(self, client=None, *, max_conversations=8, clock=time.monotonic, speech=None):
        self.client = client or ChatClient()
        self.speech = speech
        self.clock = clock
        self.max_conversations = max_conversations
        self.lock = threading.RLock()
        self.conversations = {}
        self.jobs = queue.Queue(maxsize=8)
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._work, name="speech-dialogue", daemon=True)
        self.worker.start()

    def create(self):
        with self.lock:
            if len(self.conversations) >= self.max_conversations:
                idle = [key for key, item in self.conversations.items()
                        if all(turn["state"] not in ("queued", "thinking", "answering") for turn in item["turns"])]
                if not idle:
                    raise ValueError("对话连接过多，请稍后重试")
                del self.conversations[idle[0]]
            token = secrets.token_urlsafe(24)
            self.conversations[token] = {"turns": [], "revision": 0}
            return self.snapshot(token)

    def _conversation(self, token):
        if not isinstance(token, str) or token not in self.conversations:
            raise ValueError("对话已失效，请刷新手机页面")
        return self.conversations[token]

    def validate(self, token):
        with self.lock:
            self._conversation(token)

    def snapshot(self, token):
        with self.lock:
            conversation = self._conversation(token)
            public = [{key: value for key, value in turn.items() if key != "cancel"}
                      for turn in conversation["turns"]]
            return {"dialogue_id": token, "revision": conversation["revision"],
                    "model": self.client.model, "voice_ready": self.speech is not None, "turns": copy.deepcopy(public)}

    def submit(self, token, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("请输入 1–2000 个字符的问题")
        with self.lock:
            if self.closed.is_set():
                raise ValueError("对话服务正在停止")
            conversation = self._conversation(token)
            pending = sum(turn["state"] in ("queued", "thinking", "answering") for turn in conversation["turns"])
            turn = {"id": secrets.token_hex(8), "user": text.strip(), "assistant": "",
                    "state": "queued", "error": "", "cancel": threading.Event(),
                    "created_at": self.clock()}
            if pending >= 4 or self.jobs.full():
                turn.update(state="error", error="问题积压较多，请等待回答或点击停止回答")
            else:
                self.jobs.put_nowait((token, turn))
            conversation["turns"].append(turn)
            # Keep recent completed turns, while preserving all active jobs.
            while len(conversation["turns"]) > 20:
                first = conversation["turns"][0]
                if first["state"] in ("queued", "thinking", "answering"):
                    break
                conversation["turns"].pop(0)
            conversation["revision"] += 1
            return self.snapshot(token)

    def cancel(self, token, *, reset=False, turn_ids=None):
        if turn_ids is not None and (not isinstance(turn_ids, list) or len(turn_ids) > 20 or not all(isinstance(item, str) for item in turn_ids)):
            raise ValueError("待停止的回答编号无效")
        with self.lock:
            conversation = self._conversation(token)
            for turn in conversation["turns"]:
                if turn_ids is not None and turn["id"] not in turn_ids and not reset:
                    continue
                turn["cancel"].set()
                if turn["state"] in ("queued", "thinking", "answering"):
                    turn["state"] = "cancelled"
            if reset:
                conversation["turns"] = []
            conversation["revision"] += 1
            return self.snapshot(token)

    def synthesize(self, token, turn_id, start, end):
        if self.speech is None:
            raise ValueError("离线语音模型尚未准备")
        if type(start) is not int or type(end) is not int or not 0 <= start < end or end - start > 160:
            raise ValueError("朗读范围无效，每段最多 160 个字符")
        with self.lock:
            conversation = self._conversation(token)
            turn = next((turn for turn in conversation["turns"] if turn["id"] == turn_id), None)
            if turn is None or turn["cancel"].is_set() or end > len(turn["assistant"]):
                raise ValueError("这段回答已停止或尚未生成")
            text, cancelled = turn["assistant"][start:end], turn["cancel"]
        return self.speech.synthesize(text, cancelled)

    def _messages(self, conversation, current):
        pairs, size = [], 0
        for previous in reversed(conversation["turns"]):
            if previous is current or previous["state"] != "done":
                continue
            pair_size = len(previous["user"]) + len(previous["assistant"])
            if size + pair_size > 3000 or len(pairs) >= 6:
                break
            pairs.append(previous)
            size += pair_size
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in reversed(pairs):
            messages.extend([{"role": "user", "content": turn["user"]},
                             {"role": "assistant", "content": turn["assistant"]}])
        messages.append({"role": "user", "content": current["user"]})
        return messages

    def _work(self):
        while not self.closed.is_set():
            try:
                token, turn = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    if turn["cancel"].is_set() or token not in self.conversations:
                        continue
                    conversation = self.conversations[token]
                    messages = self._messages(conversation, turn)
                    turn["state"], turn["started_at"] = "thinking", self.clock()
                    conversation["revision"] += 1
                for text in self.client.stream(messages, turn["cancel"]):
                    with self.lock:
                        if turn["cancel"].is_set():
                            break
                        if not turn["assistant"]:
                            turn["first_token_seconds"] = self.clock() - turn["started_at"]
                        turn["assistant"] += text
                        turn["state"] = "answering"
                        conversation["revision"] += 1
                with self.lock:
                    if not turn["cancel"].is_set():
                        if not turn["assistant"].strip():
                            raise RuntimeError("模型未返回可显示的回答，请检查非思考模式配置")
                        turn["state"] = "done"
                        turn["elapsed_seconds"] = self.clock() - turn["started_at"]
                        conversation["revision"] += 1
            except Exception as exc:
                # A failed model response must not terminate the ASR service.
                with self.lock:
                    if not turn["cancel"].is_set():
                        turn.update(state="error", error=str(exc))
                        self.conversations[token]["revision"] += 1
            finally:
                self.jobs.task_done()

    def close(self):
        self.closed.set()
        with self.lock:
            for token in self.conversations:
                self.cancel(token)
        self.worker.join(timeout=0.5)
