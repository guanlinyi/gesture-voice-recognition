"""Reuse phone ASR transport/TLS; isolate confirmation utterances by request ID."""
import json
from pathlib import Path
import ssl
import threading
from urllib.parse import parse_qs, urlsplit

from .asr import phone as base, asr as streaming, phone_tls as tls
HERE = Path(__file__).resolve().parent


class ConfirmationSessions(base.PhoneSessions):
    def __init__(self, recognizer, confirmation, *, router=None, **kwargs):
        if router is not None:
            kwargs["dialogue"] = router.dialogue
        super().__init__(recognizer, lambda event: None, **kwargs)
        self.confirmation = confirmation
        self.router = router
        self.request = threading.local()
        self._request_lock = threading.RLock()

    def start(self, sample_rate, dialogue_id=None):
        # Keep start and controller binding in the same ASR critical section.
        with self.request_lock:
            result = super().start(sample_rate, dialogue_id)
            self.asr_tag = None
            self.confirmation.connect(result["session_id"])
            if self.router:
                self.router.connect(result["session_id"])
            return result

    def state(self, session_id):
        result = self.confirmation.snapshot(session_id)
        if self.router:
            result["chat"] = self.router.snapshot()
        return result

    @property
    def request_lock(self):
        # Serializes lifecycle operations with audio, while base owns its ASR lock.
        return self._request_lock

    def _publish(self, events, dialogue_id=None):
        session = getattr(self.request, "session", None)
        request_id = getattr(self.request, "id", None)
        for event in events:
            self.confirmation.record_transcript(session, request_id, event)
            self.confirmation.direct_transcript(session, request_id, event)
            self.confirmation.transcript(session, request_id, event)
        return {"events": [event.to_dict() for event in events]}

    def audio_for_request(self, session_id, sequence, body, request_id):
        with self.request_lock:
            self.request.session, self.request.id = session_id, request_id
            try:
                # Reset on a new accepted command/confirmation window. Queued old tags stay muted.
                allowed = self.confirmation.accepts_audio(session_id, request_id)
                if allowed and request_id != self.asr_tag:
                    with self.lock:
                        current = self._current(session_id)
                        if sequence == current["sequence"]:
                            current["asr"] = streaming.StreamingASR(self.recognizer)
                            self.asr_tag = request_id
                muted = bytes(len(body)) if not allowed else body
                result = super().audio(session_id, sequence, muted)
                self.confirmation.heartbeat(session_id)
                return result
            finally:
                self.request.session = self.request.id = None

    def listen(self, session_id, request_id, playback_token):
        with self.request_lock, self.lock:
            current = self._current(session_id)
            self.confirmation.listen(session_id, request_id, playback_token)
            # Discard all pre-prompt hypotheses; final answers must start in this window.
            current["asr"] = streaming.StreamingASR(self.recognizer)
            self.asr_tag = request_id

    def stop(self, session_id, *, abort=False):
        with self.request_lock:
            with self.lock:
                self._current(session_id)  # an old phone must not cancel a new phone
                self.confirmation.disconnect(session_id)
                if self.router:
                    self.router.disconnect(session_id)
            # Never commit a stop-flushed "yes" after the user ends capture.
            return super().stop(session_id, abort=True)


def handler_for(sessions, ca):
    Parent = base.handler_for(sessions, ca)

    class Handler(Parent):
        def do_GET(self):
            request = urlsplit(self.path)
            if request.path == "/":
                self.reply(200, (HERE / "web/input_html/index.html").read_bytes(), "text/html; charset=utf-8")
            elif request.path == "/confirmation.js":
                self.reply(200, (HERE / "web/input_js/confirmation.js").read_bytes(), "text/javascript; charset=utf-8")
            elif request.path == "/chat.js":
                self.reply(200, (HERE / "web/input_js/chat.js").read_bytes(), "text/javascript; charset=utf-8")
            elif request.path in ("/api/confirmation/status", "/api/confirmation/prompt"):
                try:
                    query = parse_qs(request.query)
                    session = query.get("session", [None])[0]
                    with sessions.request_lock, sessions.lock:
                        sessions._current(session)
                        if request.path.endswith("/prompt"):
                            audio = sessions.confirmation.prompt_audio(session, query.get("request", [None])[0])
                            self.reply(200, audio, "audio/wav")
                        else:
                            self.reply(200, sessions.state(session))
                except (ValueError, RuntimeError) as exc:
                    self.reply(400, {"error": str(exc)})
            else:
                super().do_GET()

        def do_POST(self):
            request = urlsplit(self.path)
            if request.path != "/api/audio" and not request.path.startswith("/api/confirmation/"):
                return super().do_POST()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 192000:
                    self.close_connection = True
                    raise ValueError("请求长度无效")
                body = self.rfile.read(length)
                if len(body) != length:
                    self.close_connection = True
                    raise ValueError("音频请求未完整到达")
                if request.path == "/api/audio":
                    query = parse_qs(request.query)
                    result = sessions.audio_for_request(query.get("session", [None])[0],
                                                        int(query.get("sequence", ["-1"])[0]), body,
                                                        query.get("tag", [None])[0])
                else:
                    data = json.loads(body)
                    sid = data.get("session_id")
                    with sessions.request_lock:
                        with sessions.lock:
                            sessions._current(sid)
                        if request.path == "/api/confirmation/ready":
                            sessions.confirmation.ready(sid)
                        elif request.path == "/api/confirmation/pause":
                            sessions.confirmation.pause(sid)
                            if sessions.router:
                                sessions.router.stop_reply()
                        elif request.path == "/api/confirmation/listen":
                            sessions.listen(sid, data.get("request_id"), data.get("playback_token"))
                        elif request.path == "/api/confirmation/cancel":
                            sessions.confirmation.cancel(sid, data.get("request_id"))
                        else:
                            raise ValueError("路径不存在")
                        result = sessions.state(sid)
                self.reply(200, result)
            except (ValueError, TypeError, AttributeError) as exc:
                self.reply(getattr(exc, "status", 400), {"error": str(exc)})
            except RuntimeError as exc:
                self.reply(500, {"error": f"识别失败：{exc}"})

    return Handler


def start_server(recognizer, confirmation, *, host="0.0.0.0", port=8765, addresses=None, router=None):
    addresses = tls.resolve_addresses(addresses)
    cert, key, ca = tls.certificate_for(addresses)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    sessions = ConfirmationSessions(recognizer, confirmation, router=router)
    server = base.PhoneServer((host, port), handler_for(sessions, ca))
    try:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .2}, daemon=True)
        worker.start()
    except BaseException:
        server.server_close()
        raise
    import sys
    print("手机 Chrome 打开电脑地址（复用已有 CA，先停止旧语音服务）：", file=sys.stderr)
    for address in addresses:
        print(f"https://{address}:{port}/", file=sys.stderr)
    return server
