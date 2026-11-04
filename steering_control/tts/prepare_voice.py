"""Prepare the offline Chinese voice, with resumable archive download."""
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
from urllib.request import Request, urlopen
import uuid

from ..paths import DATA
HERE = DATA / "tts"
from ..chat import download
from .speech import MODEL_NAME, MODEL_URL, MODEL_ARCHIVE_SHA256, default_model, validate_model


def fresh(purpose):
    path = HERE / purpose / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=False)
    return path


def record(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)


def remote_size(opener=urlopen):
    request = Request(MODEL_URL, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"})
    with opener(request, timeout=60) as response:
        match = re.fullmatch(r"bytes 0-\d+/(\d+)", response.headers.get("Content-Range", ""))
        if response.status == 206 and not match:
            raise ValueError("服务器没有返回有效的压缩包总大小")
        size = int(match.group(1)) if match else int(response.headers.get("Content-Length", "0"))
    if not 0 < size < 2 * 1024**3:
        raise ValueError("无法核实语音模型压缩包大小，已停止")
    return size


def extract_new(archive, target):
    with tarfile.open(archive, "r:bz2") as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != MODEL_NAME
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f"压缩包路径或类型不受支持：{member.name}")
        for member in members:
            path = target.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, path.open("xb") as output:
                    shutil.copyfileobj(incoming, output)


def main():
    existing = default_model()
    if existing:
        print(f"语音模型已准备：{existing}\n接下来运行 python run.py。")
        return 0
    candidates = sorted(HERE.glob(f"archives/*/input_bz2/{MODEL_NAME}.tar.bz2"),
                        key=lambda p: p.stat().st_size, reverse=True)
    archive = candidates[0] if candidates else fresh("archives") / "input_bz2" / (MODEL_NAME + ".tar.bz2")
    archive.parent.mkdir(parents=True, exist_ok=True)
    size = remote_size()
    print(f"准备离线中文声音，压缩包 {size / 1024**2:.1f} MiB，支持断点续传。", flush=True)
    download.download_file(MODEL_URL, archive, size)
    digest = hashlib.sha256()
    with archive.open("rb") as incoming:
        for data in iter(lambda: incoming.read(4 * 1024 * 1024), b""):
            digest.update(data)
    if digest.hexdigest() != MODEL_ARCHIVE_SHA256:
        raise ValueError("TTS 模型压缩包 SHA256 不匹配，文件已保留，未启用")
    target = fresh("models")
    extract_new(archive, target / "input_onnx")
    model = validate_model(target / "input_onnx" / MODEL_NAME)
    record(target / "output_json/ready.json", {"model_directory": str(model), "source": MODEL_URL,
           "archive_bytes": size, "archive_sha256": digest.hexdigest(),
           "verification": "核对实测版本的固定摘要及压缩包解码；该摘要不是上游独立签名"})
    print(f"离线声音已准备：{model}\n接下来运行 python run.py。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        print(f"语音准备未完成：{exc}\n已有文件保留，重运行相同命令可续传。", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n已暂停，已有部分保留，可用相同命令续传。", file=sys.stderr)
        raise SystemExit(130)
