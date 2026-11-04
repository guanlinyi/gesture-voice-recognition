"""Streaming ASR adapter; no microphone, recording, or command execution on import."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import time

import numpy as np


from .config import DEFAULT_MODEL


def model_files(directory: Path) -> dict[str, str]:
    """Reject ambiguous model sets rather than mixing different checkpoints."""
    directory = Path(directory).resolve()
    result = {}
    for component in ("encoder", "decoder", "joiner"):
        candidates = sorted(directory.glob(f"{component}*.onnx"))
        if len(candidates) != 1:
            raise ValueError(f"{directory}: 需要唯一的 {component}*.onnx，找到 {len(candidates)} 个")
        result[component] = str(candidates[0])
    tokens = directory / "tokens.txt"
    if not tokens.is_file():
        raise ValueError(f"缺少词表：{tokens}")
    result["tokens"] = str(tokens)
    return result


def create_recognizer(directory: Path = DEFAULT_MODEL, num_threads: int = 2):
    if num_threads < 1:
        raise ValueError("线程数必须大于 0")
    files = model_files(directory)
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError("缺少 sherpa-onnx；先按 README 安装独立运行依赖") from exc
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        **files, num_threads=num_threads, sample_rate=16000,
        feature_dim=80, provider="cpu", decoding_method="greedy_search",
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=0.8,
        rule3_min_utterance_length=20.0,
    )


@dataclass(frozen=True)
class Transcript:
    type: str  # partial may be revised; final closes this utterance
    text: str
    utterance_id: int
    audio_seconds: float
    emitted_at: float  # monotonic clock, not wall time

    def to_dict(self):
        return asdict(self)


class StreamingASR:
    """One session per input stream. Call feed()/finish() in one thread.

    Input: mono floating-point samples in [-1, 1], with their actual sample rate.
    sherpa's feature extractor handles sample-rate conversion. The adapter never
    labels partial text as a confirmed command and never saves raw audio.
    """

    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()
        self.sample_rate = None
        self.audio_seconds = 0.0
        self.utterance_id = 0
        self.last_partial = ""
        self.closed = False

    def _event(self, kind, text):
        return Transcript(kind, text, self.utterance_id, self.audio_seconds, time.monotonic())

    def _decode(self, *, finishing=False):
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        text = self.recognizer.get_result(self.stream).strip()
        endpoint = finishing or self.recognizer.is_endpoint(self.stream)
        events = []
        if endpoint:
            if text:
                events.append(self._event("final", text))
            elif self.last_partial:
                # A decoder may retract all tentative text at an endpoint.
                events.append(self._event("final", ""))
            self.recognizer.reset(self.stream)
            self.utterance_id += 1
            self.last_partial = ""
        elif text != self.last_partial:
            events.append(self._event("partial", text))
            self.last_partial = text
        return events

    def feed(self, samples, sample_rate: int) -> list[Transcript]:
        if self.closed:
            raise RuntimeError("这个识别会话已经结束")
        if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or not 8000 <= sample_rate <= 96000:
            raise ValueError("采样率必须是 8000–96000 之间的整数")
        if self.sample_rate is not None and sample_rate != self.sample_rate:
            raise ValueError("同一会话不能中途改变采样率")
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 1 or not np.isfinite(samples).all():
            raise ValueError("需要有限数值组成的单声道一维音频")
        if samples.size and np.max(np.abs(samples)) > 1.001:
            raise ValueError("浮点音频必须归一化到 [-1, 1]")
        if not samples.size:
            return []
        self.sample_rate = sample_rate
        self.audio_seconds += samples.size / sample_rate
        self.stream.accept_waveform(sample_rate, np.ascontiguousarray(samples))
        return self._decode()

    def finish(self) -> list[Transcript]:
        if self.closed:
            return []
        self.closed = True
        if self.sample_rate is None:
            return []
        # Required by streaming Zipformer to flush its final chunk; not counted
        # as observed audio duration. No additional data is read from the mic.
        self.stream.accept_waveform(self.sample_rate, np.zeros(int(self.sample_rate * 0.5), np.float32))
        self.stream.input_finished()
        return self._decode(finishing=True)
