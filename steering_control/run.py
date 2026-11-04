"""CAM4 手势识别、安卓传声、语音确认与本地中文聊天。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from uuid import uuid4

from .paths import DATA, setting
from . import confirmation, model as model_module
from .asr import runtime as speech_run
from .chat import deployment


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--camera", type=int, default=4)
    inputs.add_argument("--stream", "--droidcam", help="DroidCam HTTP 视频流地址")
    parser.add_argument("--auto-camera", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model", type=Path, default=model_module.DEFAULT_MANIFEST, help="手势数值模型清单 JSON")
    parser.add_argument("--camera-init-script", type=Path, default=setting("camera_init_script"), help="可选的相机厂商初始化脚本，需支持 --check --no-preview")
    parser.add_argument("--window-size", default="1600x900")
    parser.add_argument("--max-width", type=int, default=640)
    parser.add_argument("--draw-landmarks", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--no-show", action="store_true")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--phone-ip", action="append")
    parser.add_argument("--asr-threads", type=int, default=2)
    parser.add_argument("--tts-threads", type=int, default=6)
    parser.add_argument("--answer-timeout", type=float, default=8.0)
    parser.add_argument("--chat-api", help="复用已运行的本地兼容 API；默认启动已有 Qwen 模型")
    parser.add_argument("--chat-model", default="local-voice-chat")
    parser.add_argument("--llm-port", type=int, default=8080)
    parser.add_argument("--format", choices=("numeric", "jsonl"), default="numeric",
                        help="终端指令输出格式，默认每次一行数字 1/2/3；完整事件仍写 JSONL 日志")
    parser.add_argument("--udp", help="可选：向 HOST:PORT 发送获准的 JSON 指令；数字 2 为 HALF 中间状态")
    parser.add_argument("--check", action="store_true", help="只查数值模型和依赖，不打开摄像头、音频或网络")
    return parser


class Output:
    def __init__(self, directory, *, dispatch=None, udp=None, format="numeric"):
        self.file = (directory / "events.jsonl").open("x", encoding="utf-8")
        self.lock = threading.Lock()
        self.callback = dispatch
        self.format = format
        self.socket, self.address = None, None
        if udp:
            import socket
            host, port = udp.rsplit(":", 1)
            self.address = (host, int(port))
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def event(self, event):
        with self.lock:
            self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.file.flush()

    def dispatch(self, event):
        if self.callback:
            self.callback(dict(event))
        if self.socket:
            self.socket.sendto(json.dumps(event, ensure_ascii=False).encode("utf-8"), self.address)
        self.event(event)
        print(event["id"] if self.format == "numeric" else json.dumps(event, ensure_ascii=False), flush=True)

    def close(self):
        if self.socket:
            self.socket.close()
        with self.lock:
            self.file.close()


def live_loop(args, model, controller, *, chat_provider=None, arm_state_provider=None, on_pause=None):
    import cv2
    from steering_control.hardware.capture import open_source
    from steering_control.hardware.gestures.classifier import GestureClassifier
    from steering_control.hardware.render import draw_hand
    from .gesture.features import describe
    source, window = None, None
    last_request = None
    last_feedback = None
    frames, started = 0, time.monotonic()
    try:
        source = open_source(stream=args.stream, camera=args.camera)
        if not args.no_show:
            from .preview import Preview
            window = Preview(args.window_size, model=model, control=controller, on_pause=on_pause)
        with GestureClassifier(model.model_path, **model.classifier_options) as recognizer:
            while args.max_frames is None or frames < args.max_frames:
                ok, frame = source.read()
                if not ok:
                    raise RuntimeError("摄像头未收到图像，实验停止，未确认操作已取消")
                controller.camera_alive()
                if window:
                    model = window.active_model
                if arm_state_provider:
                    feedback = arm_state_provider()
                    if feedback is not None and feedback != last_feedback:
                        controller.update_arm_state(feedback["state"], moving=feedback["moving"])
                        last_feedback = dict(feedback)
                if args.max_width and frame.shape[1] > args.max_width:
                    frame = cv2.resize(frame, (args.max_width, max(1, round(frame.shape[0] * args.max_width / frame.shape[1]))))
                now = time.monotonic() - started
                reading, points = recognizer.read(frame, int(now * 1000), now)
                descriptor = describe(points, reading.world_landmarks, frame.shape[1], frame.shape[0])
                prediction = model.predict(descriptor, reading)
                if window and window.is_learning:
                    window.learning.on_frame(frame, descriptor, reading, frames / max(.1, now) if frames else 15)
                    candidate = window.learning.validation_model
                    if candidate:
                        prediction = candidate.predict(descriptor, reading)
                pending = controller.snapshot()["pending"]
                if pending and pending["id"] != last_request:
                    # A fast direct-voice submission also consumes any previous OK permission.
                    model.policy.gate.cancel()
                    last_request = pending["id"]
                if controller.learning:
                    model.policy.gate.cancel()
                    permitted, status = None, "现场学习中，控制命令已暂停"
                elif controller.awaiting:
                    model.policy.gate.cancel()
                    permitted, status = None, "语音确认中，暂停新手势触发"
                else:
                    action, status = model.policy.apply(prediction, reading, points, frame.shape[1], frame.shape[0], now)
                    permitted = action.label
                if controller.observe(permitted):
                    # Consume the OK permission; every new operation needs fresh preparation.
                    model.policy.gate.cancel()
                controller.update_inference(prediction.label, model.policy.gate.phase == "active", status)
                frames += 1
                if window:
                    if args.draw_landmarks and points is not None:
                        draw_hand(frame, points)
                    state = controller.snapshot()
                    if chat_provider:
                        state["chat"] = chat_provider()
                    if not window.update(frame, prediction, status, state):
                        break
    finally:
        controller.close()
        if source is not None:
            source.release()
        if window:
            window.destroy()


def main(argv=None, *, dispatch=None, arm_state_provider=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    size = re.fullmatch(r"(\d+)x(\d+)", args.window_size)
    if not size or int(size[1]) < 640 or int(size[2]) < 480:
        parser.error("--window-size 最小 640x480")
    if (not 1 <= args.port <= 65535 or not 1 <= args.llm_port <= 65535 or args.max_width < 0 or args.camera < 0
            or args.max_frames is not None and args.max_frames < 1
            or not 1 <= args.asr_threads <= 8 or not 1 <= args.tts_threads <= 8
            or not math.isfinite(args.answer_timeout) or args.answer_timeout <= 0):
        parser.error("端口、帧数、宽度、线程数或超时无效")
    if args.udp:
        try:
            host, port = args.udp.rsplit(":", 1)
            if not host or not 1 <= int(port) <= 65535:
                raise ValueError()
        except ValueError:
            parser.error("--udp 使用 HOST:PORT")
    output = cache = controller = server = router = dialogue = None
    owned = ExitStack()
    try:
        model = model_module.SavedGestureModel(args.model)
        from .tts import speech as voice
        voice_dir = voice.default_model()
        if voice_dir is None:
            raise ValueError("尚未准备声音，请先运行 python prepare.py speech")
        if args.check:
            ready = speech_run.check(speech_run.DEFAULT_MODEL) == 0
            print(json.dumps({"gesture_model": model.name, "workflow": model.policy.kind,
                              "reference_samples": sum(len(d["features"]) for d in model.library.demonstrations),
                              "voice_model": str(voice_dir), "voice_files": "validated",
                              "chat_deployment": {"api": args.chat_api} if args.chat_api else deployment.latest_deployment(),
                              "actions": {label: vars(action) for label, action in confirmation.ACTIONS.items()},
                              "note": "仅数值模型、文件和依赖检查；不代表手机或实物动作已验收"}, ensure_ascii=False, indent=2))
            return 0 if ready else 2
        recognizer = speech_run.create_recognizer(speech_run.DEFAULT_MODEL, args.asr_threads)
        speech = voice.SpeechService(voice_dir, num_threads=args.tts_threads)
        from .prompts import PromptCache
        cache = PromptCache(speech)
        run = DATA / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8])
        directory = run / "output_jsonl"
        directory.mkdir(parents=True, exist_ok=False)
        metadata = run / "output_json"
        metadata.mkdir()
        with (metadata / "config.json").open("x", encoding="utf-8") as stream:
            json.dump({"arguments": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}, "model": model.name,
                       "manifest": str(model.manifest_path),
                       "manifest_sha256": hashlib.sha256(model.manifest_path.read_bytes()).hexdigest(),
                       "library_sha256": hashlib.sha256(model.library_path.read_bytes()).hexdigest(),
                       "actions": {label: vars(action) for label, action in confirmation.ACTIONS.items()},
                       "dispatch": "callback" if dispatch else "udp" if args.udp else "console event only",
                       "console_format": args.format},
                      stream, ensure_ascii=False, indent=2)
        output = Output(directory, dispatch=dispatch, udp=args.udp, format=args.format)
        def submit_action(event):
            if router:
                router.stop_reply()
            output.dispatch(event)

        controller = confirmation.Confirmation(submit_action, model_name=model.name, prompts=cache,
                                               emit=output.event, answer_timeout=args.answer_timeout)
        api = args.chat_api or owned.enter_context(deployment.local_model(args.llm_port, run_directory=run))
        from . import interaction
        chat_client = interaction.dialogue_module.ChatClient(api, args.chat_model)
        intent_client = interaction.make_intent_client(api, args.chat_model)
        dialogue = interaction.SteeringDialogue(chat_client, speech=speech, control=controller)
        router = interaction.VoiceRouter(controller, intent_client, dialogue)
        controller.unmatched_handler = router.submit
        controller.partial_handler = router.cancel_interpretation
        from .phone import start_server
        server = start_server(recognizer, controller,
                    host=args.host, port=args.port, addresses=args.phone_ip, router=router)
        cache.start()
        print(f"已载入：{model.name}\n事件记录：{directory / 'events.jsonl'}", file=sys.stderr, flush=True)
        print("手机点一次开始，可自然语音下令、持续聊天，或用 OK＋工作手势确认；指令 stdout 为 1/2/3。", file=sys.stderr, flush=True)
        from .gesture.camera_startup import prepare_camera
        prepare_camera(args)
        if args.camera_init_error:
            raise RuntimeError(args.camera_init_error)
        live_loop(args, model, controller, chat_provider=router.snapshot, arm_state_provider=arm_state_provider,
                  on_pause=router.stop_reply)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"无法运行手势语音控制：{exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        if controller:
            controller.close()
        if server:
            server.shutdown()
            server.server_close()
        if cache:
            cache.close()
        if router:
            router.close()
        if dialogue:
            dialogue.close()
        owned.close()
        if output:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
