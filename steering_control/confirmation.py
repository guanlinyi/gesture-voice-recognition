"""Gesture confirmations and explicit voice commands share one submission path."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import secrets
import threading
import time
from .commands import normalize_text, parse_voice_command


@dataclass(frozen=True)
class Action:
    id: int
    command: str
    label: str

    @property
    def prompt(self):
        return f"您是想要进行方向盘的{self.label}操作吗？"


# The old classifier calls Pointing_Up 摆1字; the display calls it 食指竖起.
ACTIONS = {
    "握拳": Action(1, "FOLD", "折叠"),
    "摆1字": Action(2, "HALF", "半折叠"),
    "手掌展开": Action(3, "UNFOLD", "展开"),
}
ALIASES = {"食指竖起": "摆1字", "1字拇指": "摆1字"}
POSITIVE = {"是", "是的", "是啊", "是呀", "对", "对的", "对啊", "对呀", "没错",
            "确认", "确认执行", "确定", "确定执行", "可以", "可以的", "可以执行",
            "好", "好的", "好啊", "行", "行的", "执行", "请执行", "嗯", "嗯嗯",
            "是的请执行", "对请执行", "好的请执行", "是的确认", "对的确认"}
NEGATIVE = ("不", "别", "取消", "否", "等等", "等一下", "等会", "等一等", "暂缓",
            "算了", "先放着", "保持原样", "保持现状", "先放一放", "先缓缓", "稍后", "改天", "先搁着",
            "没必要", "没这个必要", "免了", "先这样", "以后再", "晚点", "下次")


def classify_answer(text):
    if not isinstance(text, str):
        return "unknown"
    text = normalize_text(text)
    if not text:
        return "unknown"
    if any(word in text for word in NEGATIVE):
        return "no"
    # Allow repeated/mixed affirmatives as a whole utterance, without accepting a substring.
    reachable = [False] * (len(text) + 1)
    reachable[0] = True
    for start in range(len(text)):
        if reachable[start]:
            for word in POSITIVE:
                if text.startswith(word, start):
                    reachable[start + len(word)] = True
    return "yes" if reachable[-1] else "unknown"


class Confirmation:
    def __init__(self, dispatch, *, model_name, prompts, emit=lambda event: None,
                 clock=time.monotonic, answer_timeout=8.0, prompt_timeout=30.0,
                 connection_timeout=3.0, camera_timeout=2.0):
        for value in (answer_timeout, prompt_timeout, connection_timeout, camera_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("超时必须为有限正数")
        self.dispatch, self.model_name, self.prompts = dispatch, model_name, prompts
        self.emit, self.clock = emit, clock
        self.answer_timeout, self.prompt_timeout = answer_timeout, prompt_timeout
        self.connection_timeout, self.camera_timeout = connection_timeout, camera_timeout
        self.lock = threading.RLock()
        self.session = None
        self.audio_at = self.camera_at = None
        self.enabled = False
        self.pending = None
        self.last_label = None
        self.message = "等待手机连接并开启播报与持续收音"
        self.last_action = None
        self.last_rejection = None
        self.arm_state = {"state": "未知", "moving": False, "source": "initial", "revision": 0}
        self.closed = False
        self.revision = 0
        self.command_token = None
        self.unmatched_handler = None
        self.partial_handler = None
        self.records = []
        self.record_revision = 0
        self.inference = {"gesture": "未识别", "caption": "未识别", "prepared": False,
                          "workflow": "等待准备动作", "command": None}

    def _event(self, kind, **values):
        self.revision += 1
        self.emit({"type": kind, "time": self.clock(), "model": self.model_name, **values})

    def _record(self, role, text, *, key=None, state="done", kind="message"):
        if not text:
            return
        if key:
            record = next((row for row in self.records if row["id"] == key), None)
            if record is not None:
                if record["text"] != text or record["state"] != state:
                    record.update(text=text, state=state)
                    self.record_revision += 1
                return
        self.records.append({"id": key or secrets.token_hex(8), "role": role, "text": text,
                             "state": state, "kind": kind, "at": self.clock()})
        self.records = self.records[-160:]
        self.record_revision += 1

    def record_transcript(self, session, token, event):
        with self.lock:
            if not self.accepts_audio(session, token) or event.type not in ("partial", "final"):
                return
            key = f"user:{token}:{event.utterance_id}"
            if event.type == "final" and not event.text.strip():
                old = next((row for row in self.records if row["id"] == key), None)
                if old:
                    old.update(role="system", text="上一段暂定识别被识别器撤回", state="retracted", kind="status")
                    self.record_revision += 1
                return
            self._record("user", event.text, key=key, state="partial" if event.type == "partial" else "done")

    def connect(self, session):
        with self.lock:
            self._cancel("手机会话已更换")
            self.session, self.audio_at, self.enabled = session, self.clock(), False
            self.command_token = None
            self.message = "手机已连接，等待开启播报"

    def _session(self, session):
        if not self.session or not isinstance(session, str) or not secrets.compare_digest(session, self.session):
            raise ValueError("手机连接已失效，请重新开始")

    def ready(self, session):
        with self.lock:
            self._session(session)
            self.enabled = True
            self.command_token = secrets.token_urlsafe(18)
            self.message = "持续收音：可聊天、自然语音下令，或先 OK 再做工作手势"
            self._record("system", self.message)

    def heartbeat(self, session):
        with self.lock:
            self._session(session)
            self.audio_at = self.clock()

    def camera_alive(self):
        with self.lock:
            self.camera_at = self.clock()

    def update_arm_state(self, state, *, moving=False):
        """Mechanical-arm feedback: moving=False explicitly acknowledges completion."""
        state = {"半展开": "半折叠", "FOLD": "折叠", "HALF": "半折叠", "UNFOLD": "展开"}.get(state, state)
        if state not in ("未知", "折叠", "半折叠", "展开") or type(moving) is not bool:
            raise ValueError("机械臂状态须为未知/折叠/半折叠/展开，moving 须为布尔值")
        with self.lock:
            self.arm_state = {"state": state, "moving": moving, "source": "feedback", "revision": self.arm_state["revision"] + 1}
            self._event("arm_feedback", state=state, moving=moving)
            note = f"机械臂回报：{state}（{'执行中' if moving else '已完成' if state != '未知' else '状态未知'}）"
            self._record("system", note, kind="status")
            if not self.pending or self.pending["state"] not in ("prompting", "listening", "dispatching"):
                self.message = note

    def _reject_conflict(self, action):
        opposite = {"FOLD": "展开", "UNFOLD": "折叠"}
        current = self.arm_state
        if current["moving"] and current["state"] == opposite.get(action.command):
            reason = f"当前状态：{current['state']}，无法执行{action.label}命令"
            self.last_rejection = {"request_id": secrets.token_hex(8), "action": action.label,
                                   "text": reason, "time": self.clock()}
            self.message = reason
            self._record("system", reason, kind="error")
            self._event("action_rejected", action=action.label, state=current["state"], reason=reason)
            return True
        return False

    def update_inference(self, label, prepared=False, workflow=""):
        from .presentation import inference_view
        with self.lock:
            view = inference_view(label, prepared, workflow)
            if view != self.inference:
                self.inference = view
                # Live captions need ordering too, but don't log every camera frame.
                self.revision += 1

    def disconnect(self, session):
        with self.lock:
            if session != self.session:
                return
            self._cancel("手机收音已停止，未确认操作已取消")
            self.session, self.enabled = None, False
            self.command_token = None

    def pause(self, session):
        with self.lock:
            self._session(session)
            self._cancel("收音已暂停，未确认操作已取消")
            self.enabled, self.command_token = False, None
            self._event("input_paused")

    def _cancel(self, reason, state="cancelled"):
        if self.pending and self.pending["state"] in ("prompting", "listening"):
            self.pending.update(state=state, reason=reason)
            self.command_token = secrets.token_urlsafe(18)
            self._event(state, request_id=self.pending["id"], reason=reason)
            self._record("system", reason, kind="status")
        self.message = reason

    def _expire(self):
        if not self.pending or self.pending["state"] not in ("prompting", "listening"):
            return
        now = self.clock()
        if self.closed:
            self._cancel("实验已结束")
        elif self.audio_at is None or now - self.audio_at >= self.connection_timeout:
            self._cancel("手机音频已断开，未确认操作已取消")
            self.enabled = False
        elif self.camera_at is None or now - self.camera_at >= self.camera_timeout:
            self._cancel("摄像头输入已中断，未确认操作已取消")
        elif now >= self.pending["deadline"]:
            self._cancel("确认超时，未执行；请重新做 OK 和工作手势", "expired")

    def snapshot(self, session=None):
        with self.lock:
            if session is not None:
                self._session(session)
            self._expire()
            return {"revision": self.revision, "answer_timeout": self.answer_timeout,
                    "model": self.model_name, "message": self.message, "pending": deepcopy(self.pending),
                    "last_action": deepcopy(self.last_action), "voice": self.prompts.status(),
                    "inference": deepcopy(self.inference), "input": self._audio_input(),
                    "history": deepcopy(self.records), "history_revision": self.record_revision, "now": self.clock(),
                    "arm_state": deepcopy(self.arm_state), "last_rejection": deepcopy(self.last_rejection)}

    def _audio_input(self):
        if self.closed or not self.session or not self.enabled:
            return {"mode": "muted", "token": None}
        if self.pending and self.pending["state"] in ("prompting", "listening", "dispatching"):
            return {"mode": "confirmation", "token": self.pending["id"]} if self.pending["state"] == "listening" else {"mode": "muted", "token": None}
        return {"mode": "commands", "token": self.command_token}

    @property
    def awaiting(self):
        with self.lock:
            self._expire()
            return bool(self.pending and self.pending["state"] in ("prompting", "listening", "dispatching"))

    def observe(self, label):
        """Called only with the saved model's post-OK permitted prediction."""
        with self.lock:
            self._expire()
            label = ALIASES.get(label, label)
            old, self.last_label = self.last_label, label
            if self.closed or label not in ACTIONS or label == old or self.awaiting:
                return False
            now = self.clock()
            if (not self.enabled or self.audio_at is None or now - self.audio_at >= self.connection_timeout
                    or self.camera_at is None or now - self.camera_at >= self.camera_timeout):
                self.message = "请先在手机开启播报和持续收音，再重新做 OK 和工作手势"
                return False
            if not self.prompts.status()["ready"]:
                self.message = "确认提示音尚未就绪，请等待终端提示后重新做手势"
                return False
            action = ACTIONS[label]
            if self._reject_conflict(action):
                return False
            self.pending = {"id": secrets.token_urlsafe(18), "gesture": label, "action_id": action.id,
                            "command": action.command, "action": action.label, "prompt": action.prompt,
                            "state": "prompting", "deadline": now + self.prompt_timeout,
                            "source": "gesture",
                            "playback_token": secrets.token_urlsafe(18), "listen_after": None,
                            "last_text": "", "reason": ""}
            self.message = action.prompt + " 播报结束后请回答“是的”或“取消”"
            self._event("confirmation_requested", request_id=self.pending["id"], gesture=label,
                        action=action.label, prompt=action.prompt)
            self._record("system", action.prompt, kind="question")
            return True

    def _request(self, session, request_id, state=None):
        self._session(session)
        self._expire()
        if not self.pending or request_id != self.pending["id"]:
            raise ValueError("确认请求已过期")
        if state is not None and self.pending["state"] != state:
            raise ValueError("当前确认阶段已改变，请重新做手势")
        return self.pending

    def prompt_audio(self, session, request_id):
        with self.lock:
            pending = self._request(session, request_id, "prompting")
            audio, duration = self.prompts.get(pending["gesture"])
            pending["listen_after"] = self.clock() + duration
            return audio

    def listen(self, session, request_id, playback_token):
        with self.lock:
            pending = self._request(session, request_id, "prompting")
            if playback_token != pending["playback_token"]:
                raise ValueError("播报编号不匹配")
            if pending["listen_after"] is None or self.clock() < pending["listen_after"]:
                raise ValueError("请等待确认提示音播放结束")
            pending.update(state="listening", deadline=self.clock() + self.answer_timeout)
            self.message = f"等待确认{pending['action']}：请回答“是的”或“取消”"
            self._event("listening", request_id=request_id)
            self._record("system", f"正在听你的回答，请说“是的”或“取消”（{self.answer_timeout:g} 秒）", kind="status")

    def accepts_audio(self, session, request_id):
        with self.lock:
            self._expire()
            audio_input = self._audio_input()
            return bool(session == self.session and audio_input["token"] and request_id == audio_input["token"])

    def direct_transcript(self, session, token, event):
        if event.type == "partial" and self.partial_handler:
            with self.lock:
                valid = self.accepts_audio(session, token) and self._audio_input()["mode"] == "commands"
            if valid:
                self.partial_handler()
            return
        with self.lock:
            if not self.accepts_audio(session, token) or self._audio_input()["mode"] != "commands" or event.type != "final":
                return
            command = parse_voice_command(event.text)
            if command is None:
                if self.unmatched_handler is None or not event.text.strip():
                    return
                # New utterances invalidate an older model interpretation before it can act.
                self.command_token = secrets.token_urlsafe(18)
                self.revision += 1
                permit = self.command_token
            else:
                permit = None
        if permit is not None:
            self.unmatched_handler(session, permit, event.text)
        else:
            self.submit_voice_intent(session, token, command, event.text, interpretation="rule")

    def submit_voice_intent(self, session, token, command, text, *, interpretation="model"):
        with self.lock:
            if (not self.accepts_audio(session, token) or self._audio_input()["mode"] != "commands"
                    or command not in {action.command for action in ACTIONS.values()}):
                return False
            action = next(action for action in ACTIONS.values() if action.command == command)
            self.pending = {"id": secrets.token_urlsafe(18), "gesture": "语音指令", "action_id": action.id,
                            "command": action.command, "action": action.label, "source": "voice",
                            "state": "dispatching", "prompt": "", "last_text": text, "reason": "",
                            "interpretation": interpretation}
            self._event("voice_command", request_id=self.pending["id"], text=text, action=action.label, interpretation=interpretation)
            pending, record = self.pending, self._submission(self.pending, text)
        return self._dispatch(pending, record)

    def _submission(self, pending, text):
        return {"type": "confirmed_action", "request_id": pending["id"],
                "id": pending["action_id"], "command": pending["command"], "action": pending["action"],
                "gesture": pending["gesture"], "source": pending.get("source", "gesture"),
                "confirmation": text, "model": self.model_name, "time": self.clock(),
                "interpretation": pending.get("interpretation", "confirmation")}

    def transcript(self, session, request_id, event):
        with self.lock:
            if (not self.accepts_audio(session, request_id) or not self.pending
                    or self.pending["state"] != "listening" or request_id != self.pending["id"]):
                return
            pending = self.pending
            pending["last_text"] = event.text
            if event.type != "final":
                return
            answer = classify_answer(event.text)
            self._event("answer", request_id=request_id, text=event.text, decision=answer)
            if answer == "no":
                self._cancel("已取消，未执行；请重新做 OK 和工作手势", "rejected")
                return
            if answer != "yes":
                self.message = "没有听到明确确认，请回答“是的”或“取消”"
                self._record("system", self.message, kind="status")
                return
            pending["state"] = "dispatching"  # claim once before releasing the lock
            action = self._submission(pending, event.text)
        self._dispatch(pending, action)

    def _dispatch(self, pending, action):
        request_id = pending["id"]
        with self.lock:
            definition = next(item for item in ACTIONS.values() if item.command == action["command"])
            if self._reject_conflict(definition):
                pending.update(state="rejected_state", reason=self.message)
                self.command_token = secrets.token_urlsafe(18)
                return False
            previous = deepcopy(self.arm_state)
            direction = previous["state"] if (definition.command == "HALF" and previous["moving"]
                         and previous["state"] in ("折叠", "展开")) else definition.label
            self.arm_state = {"state": direction, "target": definition.label, "moving": True, "source": "command",
                              "revision": previous["revision"] + 1}
            reservation = self.arm_state["revision"]
        try:
            self.dispatch(action)
        except Exception as exc:
            with self.lock:
                if self.arm_state["revision"] == reservation:
                    self.arm_state = {**previous, "revision": reservation + 1}
                pending.update(state="dispatch_failed", reason=str(exc))
                self.message = f"确认后的指令提交失败：{exc}；不会自动重试"
                self._event("dispatch_failed", request_id=request_id, reason=str(exc))
                self._record("system", self.message, kind="error")
                self.command_token = secrets.token_urlsafe(18)
            return False
        with self.lock:
            pending["state"] = "confirmed"
            self.last_action = action
            self.command_token = secrets.token_urlsafe(18)
            self.message = f"已提交{pending['action']}指令（{pending['action_id']}）；可继续语音下令，或重新做 OK"
            self._event("action_submitted", request_id=request_id, action=pending["action"])
            self._record("system", f"发送方向盘的{pending['action']}命令（数字 {pending['action_id']}）", kind="sent")
        return True

    def cancel(self, session, request_id):
        with self.lock:
            pending = self._request(session, request_id)
            if pending["state"] not in ("prompting", "listening"):
                raise ValueError("本次操作已结束，不能撤回已提交的指令")
            self._cancel("用户取消，未执行；请重新做 OK 和工作手势")

    def close(self):
        with self.lock:
            self.closed = True
            self._cancel("实验已结束，未确认操作已取消")
