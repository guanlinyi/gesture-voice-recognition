"""Optional vendor initialization for CAM4; no sudo/password in this project."""
import os
from pathlib import Path
import subprocess
import sys


def prepare_camera(args):
    args.camera_init_error = None
    if not args.auto_camera or args.stream or args.camera != 4 or not args.camera_init_script:
        return
    script = Path(args.camera_init_script).expanduser().resolve()
    device = Path("/dev/video4")
    try:
        if not script.is_file():
            raise RuntimeError(f"相机初始化脚本不存在：{script}")
        print("正在自动初始化 CAM4…", file=sys.stderr, flush=True)
        for arguments, timeout in ((["--check", "--no-preview"], 15), (["--no-preview"], 60)):
            result = subprocess.run(["/bin/bash", str(script), *arguments],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            if result.stdout:
                print(result.stdout, end="", file=sys.stderr)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            if result.returncode:
                raise RuntimeError(f"相机初始化失败，退出码 {result.returncode}")
        if not device.is_char_device() or not os.access(device, os.R_OK | os.W_OK):
            raise RuntimeError(f"初始化后 {device} 不存在或没有读写权限")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        args.camera_init_error = str(exc)
