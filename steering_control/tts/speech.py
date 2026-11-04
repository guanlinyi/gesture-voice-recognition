"""Offline Chinese sentence synthesis; PCM exists only in memory by default."""
from __future__ import annotations

import io
import json
from pathlib import Path
import threading
import sys
import time
import wave

import numpy as np

from ..paths import DATA, setting
HERE = DATA / "tts"
MODEL_NAME = "vits-melo-tts-zh_en"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{MODEL_NAME}.tar.bz2"
# Digest of the archive used in the validated deployment.
MODEL_ARCHIVE_SHA256 = "e58351ed7149f290a54534538badd4077cdbe6fddc964b24d0bee870415d1514"


def validate_model(directory):
    directory = Path(directory)
    for name in ("model.onnx", "tokens.txt", "lexicon.txt", "date.fst", "number.fst", "phone.fst"):
        if not (directory / name).is_file() or (directory / name).stat().st_size == 0:
            raise ValueError(f"语音模型缺少文件：{directory / name}")
    if not (directory / "dict").is_dir():
        raise ValueError("语音模型缺少中文分词字典 dict/")
    return directory


def default_model():
    configured = setting("tts_model")
    if configured:
        return validate_model(configured)
    records = sorted(HERE.glob("models/*/output_json/ready.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for record in records:
        try:
            return validate_model(json.loads(record.read_text())["model_directory"])
        except (ValueError, OSError, KeyError):
            continue
    return None


def pcm_wav(samples, sample_rate):
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1 or not samples.size or not np.isfinite(samples).all():
        raise RuntimeError("语音模型没有返回有效声音")
    if not 8000 <= sample_rate <= 96000:
        raise RuntimeError("语音采样率不受支持")
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return result.getvalue()


class SpeechService:
    def __init__(self, directory, *, factory=None, num_threads=6):
        if type(num_threads) is not int or not 1 <= num_threads <= 8:
            raise ValueError("语音线程数需要在 1–8 之间")
        self.directory = validate_model(directory)
        self.factory = factory
        self.engine = None
        self.lock = threading.Lock()
        self.num_threads = num_threads
        self.warmed = False
        self.warmup_thread = None
        self.last_metrics = {}

    def _load(self):
        if self.factory:
            return self.factory()
        import sherpa_onnx
        folder = self.directory
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(folder / "model.onnx"), tokens=str(folder / "tokens.txt"),
                    lexicon=str(folder / "lexicon.txt")),
                num_threads=self.num_threads, provider="cpu", debug=False),
            rule_fsts=",".join(str(folder / name) for name in ("date.fst", "number.fst", "phone.fst")),
            max_num_sentences=1)
        if not config.validate():
            raise RuntimeError("中文语音合成配置不完整，请重新检查语音模型目录")
        return sherpa_onnx.OfflineTts(config)

    def warmup(self):
        started = time.monotonic()
        with self.lock:
            if self.engine is None:
                self.engine = self._load()
            if not self.warmed:
                self.engine.generate("你好。", sid=0, speed=1.0, callback=lambda samples, progress: 1)
                self.warmed = True
        return time.monotonic() - started

    def warmup_background(self):
        if self.warmup_thread is not None:
            return

        def run():
            try:
                print("正在后台预热中文声音…", file=sys.stderr, flush=True)
                seconds = self.warmup()
                print(f"中文声音预热完成（{seconds:.1f} 秒，{self.num_threads} 线程）", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"声音预热未完成：{exc}；首次朗读时会重试", file=sys.stderr, flush=True)

        self.warmup_thread = threading.Thread(target=run, name="speech-warmup", daemon=True)
        self.warmup_thread.start()

    def synthesize(self, text, cancelled):
        if not isinstance(text, str) or not text.strip() or len(text) > 160:
            raise ValueError("每段语音需要 1–160 个字符")
        requested = time.monotonic()
        with self.lock:
            acquired = time.monotonic()
            if cancelled.is_set():
                raise ValueError("这段回答已停止")
            if self.engine is None:
                self.engine = self._load()
            if cancelled.is_set():
                raise ValueError("这段回答已停止")
            generated = time.monotonic()
            audio = self.engine.generate(text, sid=0, speed=1.0,
                                         callback=lambda samples, progress: 0 if cancelled.is_set() else 1)
            if cancelled.is_set():
                raise ValueError("这段回答已停止")
            finished = time.monotonic()
            samples, rate = np.asarray(audio.samples), int(audio.sample_rate)
            result = pcm_wav(samples, rate)
            self.warmed = True
            duration = samples.size / rate
            self.last_metrics = {"characters": len(text), "threads": self.num_threads,
                                 "queue_seconds": acquired - requested,
                                 "load_seconds": generated - acquired,
                                 "synthesis_seconds": finished - generated,
                                 "audio_seconds": duration,
                                 "rtf": (finished - generated) / duration}
            print(f"语音合成：{len(text)} 字，耗时 {finished - generated:.2f} 秒，声音 {duration:.2f} 秒", file=sys.stderr, flush=True)
            return result
