"""Prepare public model files; downloaded bytes remain available for resumption."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
from urllib.request import Request, urlopen
from uuid import uuid4

from .paths import DATA, GESTURE_MODEL, ROOT
from .chat.download import download_file

GESTURE_URL = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_size(url):
    request = Request(url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"})
    with urlopen(request, timeout=60) as response:
        match = re.fullmatch(r"bytes 0-\d+/(\d+)", response.headers.get("Content-Range", ""))
        if response.status == 206 and not match:
            raise ValueError("服务器未返回有效总大小")
        size = int(match.group(1)) if match else int(response.headers.get("Content-Length", "0"))
    if not 0 < size < 4 * 1024**3:
        raise ValueError("无法核实模型下载大小")
    return size


def gesture():
    manifest = json.loads((ROOT / "assets/output_json/model.json").read_text())
    expected = manifest["network"]["sha256"]
    if GESTURE_MODEL.is_file() and hashlib.sha256(GESTURE_MODEL.read_bytes()).hexdigest() == expected:
        print("手势网络已准备")
        return
    GESTURE_MODEL.parent.mkdir(parents=True, exist_ok=True)
    download_file(GESTURE_URL, GESTURE_MODEL, remote_size(GESTURE_URL))
    if hashlib.sha256(GESTURE_MODEL.read_bytes()).hexdigest() != expected:
        raise ValueError("手势网络 SHA256 不匹配，文件已保留，请检查来源")
    print(f"手势网络已准备：{GESTURE_MODEL}")


def extract_model(archive, destination, name):
    """Allow only unique regular files/directories below the named model root."""
    with tarfile.open(archive, "r:bz2") as source:
        members, seen = source.getmembers(), set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != name
                    or not (member.isdir() or member.isfile()) or member.isfile() and path in seen):
                raise ValueError(f"不支持的压缩包路径或类型：{member.name}")
            seen.add(path)
        (destination / name).mkdir(parents=True, exist_ok=False)
        for member in members:
            output = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, output.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)


def streaming_asr():
    from .asr.asr import model_files
    from .asr.config import DEFAULT_MODEL, HERE, MODEL_NAME, MODEL_URL, MODEL_ARCHIVE_SHA256
    if DEFAULT_MODEL.exists():
        model_files(DEFAULT_MODEL)
        print(f"流式识别模型已准备：{DEFAULT_MODEL}")
        return
    archive = HERE / "input_bz2" / (MODEL_NAME + ".tar.bz2")
    archive.parent.mkdir(parents=True, exist_ok=True)
    download_file(MODEL_URL, archive, remote_size(MODEL_URL))
    if file_sha256(archive) != MODEL_ARCHIVE_SHA256:
        raise ValueError("ASR 模型压缩包 SHA256 不匹配，文件已保留，未启用")
    # Publish only after extraction and validation. Failed attempts stay separate.
    stage = HERE / "extractions" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8])
    extract_model(archive, stage / "input_onnx", MODEL_NAME)
    ready = stage / "input_onnx" / MODEL_NAME
    model_files(ready)
    DEFAULT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    ready.rename(DEFAULT_MODEL)
    print(f"流式识别模型已准备：{DEFAULT_MODEL}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("all", "gesture", "speech", "chat"), nargs="?", default="all")
    parser.add_argument("--cpu", action="store_true", help="使用 CPU 构建 llama.cpp；默认 Jetson Orin CUDA sm87")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 12:
        parser.error("--jobs 范围 1–12")
    try:
        if args.component in ("all", "gesture"):
            gesture()
        if args.component in ("all", "speech"):
            streaming_asr()
            from .tts.prepare_voice import main as prepare_voice
            prepare_voice()
        if args.component in ("all", "chat"):
            from .chat.prepare_chat import main as prepare_chat
            return prepare_chat(["--jobs", str(args.jobs)] + (["--cpu"] if args.cpu else []))
        return 0
    except KeyboardInterrupt:
        print("已暂停，下载部分保留，重运行相同命令可续传。", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        print(f"模型准备失败：{exc}\n已有文件保留，可重运行相同命令。", file=sys.stderr)
        return 2
