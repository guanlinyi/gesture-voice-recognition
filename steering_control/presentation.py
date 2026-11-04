"""Shared live-inference captions for desktop and phone (no camera/UI imports)."""
import time

from .confirmation import ACTIONS, ALIASES

AWAITING = {"prompting", "listening", "dispatching"}
PHASES = {"prompting": "正在语音询问", "listening": "等待语音确认", "dispatching": "正在提交指令"}


class SentCommandNotice:
    """Show a successful submission once; repeated snapshots don't prolong it."""
    def __init__(self, *, clock=time.monotonic, seconds=4.0):
        self.clock, self.seconds = clock, seconds
        self.request_id, self.text, self.until = None, None, 0.0
        self.rejection_id = None
        self.color = (255, 235, 0)

    def update(self, last_action, rejection=None):
        now = self.clock()
        if last_action and last_action["request_id"] != self.request_id:
            self.request_id = last_action["request_id"]
            self.text = f"发送方向盘的{last_action['action']}命令"
            self.until = now + self.seconds
            self.color = (255, 235, 0)
        if rejection and rejection["request_id"] != self.rejection_id:
            self.rejection_id = rejection["request_id"]
            if not last_action or rejection["time"] >= last_action.get("time", 0):
                self.text, self.until, self.color = rejection["text"], now + self.seconds, (255, 110, 100)
        return self.text if now < self.until else None


def arm_caption(snapshot):
    arm = snapshot.get("arm_state", {"state": "未知", "moving": False, "source": "initial"})
    suffix = "（执行中，待机械臂回报）" if arm["moving"] and arm["source"] == "command" else "（执行中）" if arm["moving"] else "（已完成）" if arm["state"] != "未知" else ""
    return f"当前状态：{arm['state']}{suffix}"


def inference_view(label, prepared=False, workflow=""):
    label = ALIASES.get(label, label)
    gesture = {"摆1字": "食指竖起", "手掌展开": "手掌张开", "OK手势": "OK"}.get(label, label or "未识别")
    action = ACTIONS.get(label)
    if action:
        operation = "半折叠" if action.command == "HALF" else action.label
        caption = f"{gesture} - {operation}命令"
    elif label == "OK手势":
        caption = "OK - 准备状态" if prepared else "OK - 准备确认中"
    else:
        caption = gesture
    return {"gesture": gesture, "caption": caption, "prepared": bool(prepared),
            "workflow": workflow, "command": action.command if action else None}


def overlay_lines(inference, pending=None):
    if pending and pending["state"] in AWAITING:
        locked = (f"语音指令 - {pending['action']}命令" if pending.get("source") == "voice"
                  else inference_view(pending["gesture"])["caption"])
        return [f"当前识别：{inference['caption']}",
                f"本次锁定：{locked}",
                PHASES[pending["state"]] + "；结束后可语音下令或重新做 OK"]
    if inference["prepared"]:
        title = inference["caption"] if inference["command"] else "OK - 准备状态"
        return [title, "已进入准备状态", inference["workflow"] or "请做工作手势"]
    title = inference["caption"] if inference["gesture"] == "OK" else "未进入准备状态"
    return [title, f"当前识别：{inference['caption']}", "可直接语音下令；手势控制先做 OK"]


def conversation_rows(snapshot):
    """Merge actual recognized speech, control feedback and streaming chat for the PC."""
    rows = [dict(row) for row in snapshot.get("history", [])]
    heard = {row["text"] for row in rows if row["role"] == "user" and row["state"] == "done"}
    chat = snapshot.get("chat") or {}
    dialogue = chat.get("dialogue") or {}
    labels = {"queued": "等待回答…", "thinking": "正在思考…", "answering": "正在回答…", "cancelled": "回答已停止"}
    for turn in dialogue.get("turns", []):
        at = turn.get("created_at", 0.)
        if turn["user"] not in heard:
            rows.append({"id": "chat-user:" + turn["id"], "role": "user", "text": turn["user"],
                         "at": at, "state": "done", "kind": "message"})
        text = turn.get("assistant") or turn.get("error") or labels.get(turn["state"], "")
        if text:
            if turn["state"] == "cancelled" and turn.get("assistant"):
                text += "（已停止）"
            rows.append({"id": "chat-system:" + turn["id"], "role": "system", "text": text,
                         "at": at + .0001, "state": turn["state"], "kind": "error" if turn.get("error") else "message"})
    if chat.get("phase") == "error":
        rows.append({"id": "routing-error", "role": "system", "text": chat["message"],
                     "at": snapshot.get("now", 0.), "state": "error", "kind": "error"})
    return sorted(rows, key=lambda row: row["at"])[-180:]


def conversation_status(snapshot):
    pending = snapshot.get("pending")
    if pending and pending["state"] == "listening":
        left = max(0, pending["deadline"] - snapshot["now"])
        return f"正在收听确认回答 · 剩余 {left:.0f} 秒"
    if pending and pending["state"] == "prompting":
        return "正在询问，播完即可回答"
    chat = snapshot.get("chat") or {}
    if chat.get("phase") in ("understanding", "error"):
        return chat["message"]
    return snapshot.get("message", "等待连接")
