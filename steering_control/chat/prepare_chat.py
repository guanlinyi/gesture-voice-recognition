"""Prepare local model and engine in fresh experiment directories; no pip/sudo."""
import argparse
from pathlib import Path
import sys

from .deployment import HERE, build_engine, fresh_directory, prepare_model, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true", help="不用 CUDA 构建；较慢的兼容备选")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--model-file", type=Path, help="从手工下载的同名 GGUF 复制，仍核对 SHA256")
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 12:
        parser.error("--jobs 范围为 1–12")
    try:
        model = prepare_model(args.model_file)
        binary = build_engine(cpu=args.cpu, jobs=args.jobs)
        deployment = {"model": str(model), "binary": str(binary), "mode": "cpu" if args.cpu else "cuda-sm87"}
        run = fresh_directory("deployments")
        write_json(run / "output_json/runtime.json", deployment)
        print(f"对话环境已准备：{run}\n接下来运行：python run.py")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"对话准备未完成：{exc}\n已下载部分保留，下次运行相同命令自动续传；没有覆盖现有模型或修改 Python 环境。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n准备已暂停，已下载部分保留。下次运行相同命令自动续传。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
