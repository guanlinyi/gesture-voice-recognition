"""Experiment-local paths and pinned public model source (stdlib only)."""
from pathlib import Path

from ..paths import DATA, setting
HERE = DATA / "asr"
MODEL_NAME = "sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL_NAME}.tar.bz2"
# Digest of the archive used in the validated deployment.
MODEL_ARCHIVE_SHA256 = "fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74"
DEFAULT_MODEL = Path(setting("asr_model", str(HERE / "input_onnx" / MODEL_NAME)))
