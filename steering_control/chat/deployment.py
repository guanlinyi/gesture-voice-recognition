"""Pinned local llama.cpp deployment; all artifacts belong to this experiment."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
import socket
import uuid

from .download import download_file
from .http_api import api_urlopen

from ..paths import DATA, setting
HERE = DATA / "chat"
ENGINE_COMMIT = "8642541b255bfb5dc39cd351a178be09013a55d6"  # upstream v0.8.0, 2026-10-28
MODEL_FILE = "Qwen3.5-4B-Q4_K_M.gguf"
MODEL_URL = f"https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/{MODEL_FILE}"
MODEL_SHA256 = "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4"
MODEL_SIZE = 2740937888  # upstream Git LFS pointer for the pinned SHA256


def fresh_directory(purpose):
    path = HERE / purpose / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_model(local_file=None):
    candidates = sorted((HERE / "models").glob(f"*/input_gguf/{MODEL_FILE}"),
                        key=lambda path: path.stat().st_size, reverse=True)
    for model in candidates:
        if model.stat().st_size == MODEL_SIZE and sha256(model) == MODEL_SHA256:
            print(f"复用已核对模型：{model}", flush=True)
            source = model.parent.parent / "output_json/source.json"
            if not source.exists():
                _record_model_source(model, MODEL_SHA256)
            return model
    partial = next((path for path in candidates if path.stat().st_size < MODEL_SIZE), None)
    if not local_file and partial is not None:
        model = partial
    else:
        run = fresh_directory("models")
        model = run / "input_gguf" / MODEL_FILE
        model.parent.mkdir()
    if local_file:
        # Copy an explicitly supplied file; never move or overwrite the source.
        import shutil
        with Path(local_file).open("rb") as source, model.open("xb") as output:
            shutil.copyfileobj(source, output)
    else:
        print(f"下载约 2.74 GB 对话模型：{MODEL_URL}", flush=True)
        download_file(MODEL_URL, model, MODEL_SIZE)
    print("模型字节已接收，正在核对 SHA256…", flush=True)
    digest = sha256(model)
    if digest != MODEL_SHA256:
        raise ValueError(f"模型 SHA256 不匹配，文件保留在 {model}，没有启用")
    _record_model_source(model, digest)
    return model


def _record_model_source(model, digest):
    source = model.parent.parent / "output_json/source.json"
    if not source.exists():
        write_json(source, {"original": "Qwen/Qwen3.5-4B", "quantization": "unsloth/Qwen3.5-4B-GGUF",
                   "source": MODEL_URL, "sha256": digest, "size": MODEL_SIZE, "file": str(model)})


def build_engine(*, cpu=False, jobs=4):
    mode = "cpu" if cpu else "cuda-sm87"
    for metadata in sorted((HERE / "engines").glob("*/output_json/engine.json"), reverse=True):
        record = json.loads(metadata.read_text())
        binary = Path(record["binary"])
        if record.get("commit") == ENGINE_COMMIT and record.get("mode") == mode and binary.is_file():
            print(f"复用本实验引擎：{binary}", flush=True)
            return binary
    run = fresh_directory("engines")
    source, build = run / "source/llama.cpp", run / "build"
    source.parent.mkdir()
    commands = [
        ["/usr/bin/git", "clone", "--depth", "1", "--branch", "v0.8.0", "https://github.com/ggml-org/llama.cpp.git", str(source)],
        ["/usr/bin/cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
         "-DCMAKE_C_COMPILER=/usr/bin/gcc", "-DCMAKE_CXX_COMPILER=/usr/bin/g++",
         "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_APP=OFF",
         "-DLLAMA_BUILD_UI=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF", "-DLLAMA_OPENSSL=OFF",
         f"-DGGML_CUDA={'OFF' if cpu else 'ON'}"],
        ["/usr/bin/cmake", "--build", str(build), "--config", "Release", "-j", str(jobs), "--target", "llama-server"],
    ]
    if not cpu:
        commands[1] += ["-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc", "-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++",
                        "-DCMAKE_CUDA_ARCHITECTURES=87", "-DGGML_CUDA_GRAPHS=OFF", "-DGGML_CUDA_FA=OFF"]
    logs = run / "output_log"
    logs.mkdir()
    for index, command in enumerate(commands):
        print(f"准备引擎，步骤 {index + 1}/3；日志：{logs}", flush=True)
        with (logs / f"step_{index + 1}.log").open("x") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        print("仍在编译/准备，详细输出见上述日志…", flush=True)
                if process.returncode:
                    raise RuntimeError(f"引擎步骤 {index + 1} 失败，详见 {logs / f'step_{index + 1}.log'}")
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
                raise
        if index == 0:
            commit = subprocess.check_output(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            if commit != ENGINE_COMMIT:
                raise ValueError("上游源码标签与固定 commit 不一致，已停止构建")
    binary = build / "bin/llama-server"
    if not binary.is_file():
        raise RuntimeError("构建未生成 llama-server，请检查本次引擎日志")
    write_json(run / "output_json/engine.json", {"binary": str(binary), "commit": ENGINE_COMMIT, "mode": mode})
    return binary


def latest_deployment():
    binary, model = setting("llama_server"), setting("chat_model")
    if bool(binary) != bool(model):
        raise ValueError("复用已有聊天服务时，请同时配置 llama_server 和 chat_model")
    if binary and model:
        if not Path(binary).is_file() or not Path(model).is_file():
            raise ValueError("本地配置中的 llama_server 或 chat_model 不存在")
        mode = setting("chat_mode", "cuda-sm87")
        if mode not in ("cpu", "cuda-sm87"):
            raise ValueError("chat_mode 需要为 cpu 或 cuda-sm87")
        return {"binary": str(Path(binary).resolve()), "model": str(Path(model).resolve()), "mode": mode}
    for metadata in sorted((HERE / "deployments").glob("*/output_json/runtime.json"), reverse=True):
        record = json.loads(metadata.read_text())
        if Path(record["binary"]).is_file() and Path(record["model"]).is_file():
            return record
    raise RuntimeError("尚未准备对话模型。请先运行 python prepare.py chat")


def server_command(deployment, port):
    return [deployment["binary"], "--model", deployment["model"], "--alias", "local-voice-chat",
            "--host", "127.0.0.1", "--port", str(port), "--ctx-size", "8192", "--parallel", "1",
            "--threads", "4", "--n-gpu-layers", "0" if deployment["mode"] == "cpu" else "99",
            "--flash-attn", "off", "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}']


def wait_for_model(process, port, logfile, *, timeout=180, opener=None, clock=time.monotonic, sleep=time.sleep):
    """Probe actual HTTP readiness; keep the startup phase visible to the user."""
    opener = opener if opener is not None else api_urlopen
    started = clock()
    last_report = started
    last_status = "等待本地模型监听端口"
    while True:
        if process.poll() is not None:
            raise RuntimeError(f"对话模型启动失败。检查 {logfile}；如 CUDA 不兼容，可运行 prepare.py chat --cpu")
        try:
            with opener(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if json.load(response).get("status") == "ok":
                    with opener(f"http://127.0.0.1:{port}/v1/models", timeout=2) as models:
                        aliases = [item["id"] for item in json.load(models).get("data", [])]
                    if "local-voice-chat" not in aliases:
                        raise RuntimeError("对话端口响应的模型与本次启动不一致，请换 --llm-port")
                    print(f"对话模型已就绪（{clock() - started:.1f} 秒），正在启动手机语音网页…", file=sys.stderr, flush=True)
                    return
                last_status = "本地模型尚未就绪"
        except HTTPError as exc:
            last_status = "模型正在初始化" if exc.code == 503 else f"本地检查返回 HTTP {exc.code}"
        except (URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            last_status = "本机 HTTP 接口响应超时" if isinstance(reason, TimeoutError) else "本机 HTTP 接口暂未连接成功"
        elapsed = clock() - started
        if elapsed >= timeout:
            raise RuntimeError(f"对话模型在 {timeout} 秒内未就绪（{last_status}）。查看 {logfile}")
        if clock() - last_report >= 5:
            print(f"启动检测已等待 {elapsed:.0f} 秒：{last_status}；详细日志：{logfile}", file=sys.stderr, flush=True)
            last_report = clock()
        sleep(0.5)


@contextmanager
def local_model(port=8080, *, run_directory=None):
    deployment = latest_deployment()
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=0.5)
    except ConnectionRefusedError:
        pass
    else:
        connection.close()
        raise RuntimeError("对话端口已被占用，请换 --llm-port，或使用 --chat-api 连接已有服务")
    run = Path(run_directory) if run_directory is not None else fresh_directory("runs")
    logs = run / "output_log"
    logs.mkdir()
    command = server_command(deployment, port)
    print(f"加载本地对话模型，日志：{logs / 'llama-server.log'}", file=sys.stderr, flush=True)
    with (logs / "llama-server.log").open("x") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            wait_for_model(process, port, logs / "llama-server.log")
            write_json(run / "output_json/session.json", {"deployment": deployment, "port": port, "command": command})
            yield f"http://127.0.0.1:{port}/v1"
        finally:
            # Only the child process created above belongs to this launcher.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
