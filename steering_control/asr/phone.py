"""Phone microphone via same-origin HTTPS; standard library, no extra pip deps."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import numpy as np

from .asr import StreamingASR
from .phone_tls import certificate_for, lan_addresses, normalize_addresses, resolve_addresses

HERE = Path(__file__).resolve().parents[1]


class ProtocolError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PhoneSessions:
    """One active phone; ordered chunks and bounded sizes, no raw audio files."""
    def __init__(self, recognizer, emit, *, clock=time.monotonic, idle_timeout=30, dialogue=None):
        self.recognizer, self.emit, self.clock = recognizer, emit, clock
        self.idle_timeout = idle_timeout
        self.lock = threading.Lock()
        self.active = None
        self.dialogue = dialogue

    def start(self, sample_rate, dialogue_id=None):
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 96000:
            raise ProtocolError("手机采样率不受支持")
        with self.lock:
            if dialogue_id is not None:
                if self.dialogue is None:
                    raise ProtocolError("当前入口未启用文字问答")
                self.dialogue.validate(dialogue_id)
            if self.active and self.clock() - self.active["last_seen"] < self.idle_timeout:
                raise ProtocolError("已有手机正在传声，请先在原页面停止，或等待 30 秒后重试", 409)
            session_id = secrets.token_urlsafe(24)
            self.active = {"id": session_id, "rate": sample_rate, "sequence": 0,
                           "last_seen": self.clock(), "asr": StreamingASR(self.recognizer), "dialogue_id": dialogue_id}
            print(f"手机麦克风已连接：{sample_rate} Hz", file=sys.stderr, flush=True)
            return {"session_id": session_id, "sample_rate": sample_rate}

    def _current(self, session_id):
        if not isinstance(session_id, str) or not self.active or not secrets.compare_digest(self.active["id"], session_id):
            raise ProtocolError("手机连接已失效，请重新开始", 410)
        if self.clock() - self.active["last_seen"] >= self.idle_timeout:
            self.active = None
            raise ProtocolError("手机传声已超时，请重新开始", 410)
        return self.active

    def _publish(self, events, dialogue_id=None):
        for event in events:
            self.emit(event)
            if self.dialogue is not None and dialogue_id and event.type == "final" and event.text.strip():
                # submit only enqueues work; model generation cannot hold the ASR lock.
                try:
                    self.dialogue.submit(dialogue_id, event.text)
                except ValueError as exc:
                    print(f"这句未送入问答：{exc}", file=sys.stderr, flush=True)
        return {"events": [event.to_dict() for event in events]}

    def audio(self, session_id, sequence, body):
        with self.lock:
            current = self._current(session_id)
            if type(sequence) is not int or sequence != current["sequence"]:
                raise ProtocolError("音频顺序不一致，已拒绝重复或缺失的数据，请停止后重新开始", 409)
            if not body or len(body) % 4 or len(body) > current["rate"] * 2:
                raise ProtocolError("音频块格式错误或超过 0.5 秒")
            samples = np.frombuffer(body, dtype="<f4")
            events = current["asr"].feed(samples, current["rate"])
            current["sequence"] += 1
            current["last_seen"] = self.clock()
            return self._publish(events, current["dialogue_id"])

    def stop(self, session_id, *, abort=False):
        with self.lock:
            current = self._current(session_id)
            events = [] if abort else current["asr"].finish()
            self.active = None
            print("手机麦克风已断开", file=sys.stderr, flush=True)
            return self._publish(events, current["dialogue_id"])


def handler_for(sessions, ca_certificate):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def reply(self, status, body, mime="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if mime == "application/x-x509-ca-cert":
                self.send_header("Content-Disposition", 'attachment; filename="orin-phone-ca.crt"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            routes = {"/": (HERE / "web/input_html/index.html", "text/html; charset=utf-8"),
                      "/capture.js": (HERE / "web/input_js/capture.js", "text/javascript; charset=utf-8"),
                      "/phone.js": (HERE / "web/input_js/phone.js", "text/javascript; charset=utf-8"),
                      "/phone-ca.crt": (ca_certificate, "application/x-x509-ca-cert")}
            path = urlsplit(self.path).path
            if sessions.dialogue is not None and path == "/dialogue.js":
                source = HERE / "web/input_js/dialogue.js"
                self.reply(200, source.read_bytes(), "text/javascript; charset=utf-8")
                return
            if sessions.dialogue is not None and path == "/reply.js":
                source = HERE / "web/input_js/reply.js"
                self.reply(200, source.read_bytes(), "text/javascript; charset=utf-8")
                return
            if sessions.dialogue is not None and path == "/api/dialogue/status":
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    self.reply(200, sessions.dialogue.snapshot(query.get("dialogue", [None])[0]))
                except ValueError as exc:
                    self.reply(410, {"error": str(exc)})
                return
            if path == "/health":
                self.reply(200, {"ready": True, "source": "phone", "stores_audio": False})
            elif path in routes:
                file, mime = routes[path]
                content = file.read_bytes()
                if path == "/" and sessions.dialogue is not None:
                    content = content.replace(b"<!-- DIALOGUE_SCRIPT -->", b'<script src="/reply.js"></script><script src="/dialogue.js"></script>')
                self.reply(200, content, mime)
            else:
                self.reply(404, {"error": "路径不存在"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 192000:
                    self.close_connection = True
                    raise ProtocolError("请求长度无效")
                body = self.rfile.read(length)
                if len(body) != length:
                    self.close_connection = True
                    raise ProtocolError("音频请求未完整到达")
                request = urlsplit(self.path)
                if request.path == "/api/start":
                    data = json.loads(body)
                    result = sessions.start(data.get("sample_rate"), data.get("dialogue_id"))
                elif request.path.startswith("/api/dialogue/") and sessions.dialogue is not None:
                    data = json.loads(body)
                    if request.path == "/api/dialogue/start":
                        result = sessions.dialogue.create()
                    elif request.path == "/api/dialogue/text":
                        result = sessions.dialogue.submit(data.get("dialogue_id"), data.get("text"))
                    elif request.path == "/api/dialogue/speech":
                        audio = sessions.dialogue.synthesize(data.get("dialogue_id"), data.get("turn_id"), data.get("start"), data.get("end"))
                        self.reply(200, audio, "audio/wav")
                        return
                    elif request.path in ("/api/dialogue/cancel", "/api/dialogue/reset"):
                        result = sessions.dialogue.cancel(data.get("dialogue_id"), reset=request.path.endswith("/reset"), turn_ids=data.get("turn_ids"))
                    else:
                        raise ProtocolError("路径不存在", 404)
                elif request.path == "/api/audio":
                    query = parse_qs(request.query)
                    result = sessions.audio(query.get("session", [None])[0], int(query.get("sequence", ["-1"])[0]), body)
                elif request.path == "/api/stop":
                    data = json.loads(body)
                    abort = data.get("abort", False)
                    if type(abort) is not bool:
                        raise ProtocolError("停止参数无效")
                    result = sessions.stop(data.get("session_id"), abort=abort)
                else:
                    raise ProtocolError("路径不存在", 404)
                self.reply(200, result)
            except (ProtocolError, ValueError, TypeError, AttributeError) as exc:
                self.reply(getattr(exc, "status", 400), {"error": str(exc)})
            except RuntimeError as exc:
                self.reply(500, {"error": f"推理失败：{exc}"})

    return Handler


class PhoneServer(ThreadingHTTPServer):
    daemon_threads = True


def serve_phone(recognizer, emit, *, host="0.0.0.0", port=8765, addresses=None, dialogue=None):
    addresses = resolve_addresses(addresses)
    cert, key, ca = certificate_for(addresses)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    sessions = PhoneSessions(recognizer, emit, dialogue=dialogue)
    with PhoneServer((host, port), handler_for(sessions, ca)) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print("\n安卓手机和电脑连接同一 Wi-Fi，用 Chrome 打开以下电脑地址：", file=sys.stderr)
        for address in addresses:
            print(f"  https://{address}:{port}/", file=sys.stderr)
        print(f"首次使用：下载页面上的证书并按说明安装为 CA 证书。\n本地 CA 文件：{ca}\n点击“开始传声”，允许麦克风；按 Ctrl+C 关闭电脑服务。", file=sys.stderr, flush=True)
        if dialogue is not None:
            print("文字问答已启用：说完一句后自动回答，也可在手机输入文字。", file=sys.stderr, flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\n手机传声服务已停止", file=sys.stderr)
