"""直接读取 HTTP MJPEG；不经过系统代理或 Windows 虚拟摄像头。"""
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import cv2
import numpy as np


class DroidCamStream:
    def __init__(self, url, timeout=5):
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("视频流地址必须以 http:// 或 https:// 开头。")
        self.response = None
        self.buffer = bytearray()
        self.timeout = timeout
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._reader = None
        self._latest = None
        self._sequence = 0
        self._consumed = 0
        self._delivered = 0
        self._skipped = 0
        self._frame_age_ms = 0.0
        self._ended = False
        self._error = None
        self._reported_error = False
        try:
            # 空 ProxyHandler 显式忽略环境变量和 Windows 的 HTTP 代理设置。
            opener = build_opener(ProxyHandler({}))
            self.response = opener.open(Request(url), timeout=timeout)
            content_type = self.response.headers.get("Content-Type", "").lower()
            if "multipart/" not in content_type and "image/jpeg" not in content_type:
                detail = self.response.read(8192).decode("utf-8", errors="replace")
                if "busy" in detail.lower():
                    raise ValueError("DroidCam is Busy：请先在电脑 DroidCam 客户端停止连接，手机 App 保持开启，再重试。")
                raise ValueError(f"地址未返回 MJPEG 视频（Content-Type={content_type}），请检查 /video 路径。")
            deadline = time.monotonic() + self.timeout
            encoded = None
            first_frame = None
            while time.monotonic() < deadline:
                encoded = self._read_jpeg(deadline)
                if encoded is None:
                    break
                first_frame = self._decode(encoded)
                if first_frame is not None:
                    break
            if first_frame is None:
                raise ValueError("已连接视频地址，但没有解码到画面。")
            self._sequence = 1
            self._latest = (encoded, time.monotonic())
            # 接收不等待模型或绘图；只保留最新的压缩帧，按需解码。
            self._reader = threading.Thread(target=self._receive, name="DroidCamReceiver", daemon=True)
            self._reader.start()
        except Exception:
            self.release()
            raise

    @staticmethod
    def _decode(encoded):
        return cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)

    def _read_jpeg(self, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            start = self.buffer.find(b"\xff\xd8")
            if start >= 0:
                if start:
                    del self.buffer[:start]
                end = self.buffer.find(b"\xff\xd9", 2)
                if end >= 0:
                    encoded = bytes(self.buffer[:end + 2])
                    del self.buffer[:end + 2]
                    return encoded
            chunk = self.response.read1(65536)
            if not chunk:
                return None
            self.buffer.extend(chunk)
            if len(self.buffer) > 16 * 1024 * 1024:
                raise ValueError("视频数据超过单帧缓冲上限，可能不是支持的 MJPEG 流。")
        if not self._stop.is_set():
            raise TimeoutError("等待 DroidCam 视频帧超时。")
        return None

    def _receive(self):
        try:
            while not self._stop.is_set():
                encoded = self._read_jpeg()
                if encoded is None:
                    break
                with self._condition:
                    self._sequence += 1
                    self._latest = (encoded, time.monotonic())
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                self._error = exc
        finally:
            with self._condition:
                self._ended = True
                self._condition.notify_all()

    def read(self):
        deadline = time.monotonic() + self.timeout
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._sequence > self._consumed or self._ended or self._stop.is_set(),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if self._stop.is_set():
                    return False, None
                if self._sequence <= self._consumed:
                    error = self._error
                    if not self._ended:
                        error = TimeoutError("等待 DroidCam 新画面超时。")
                    if error is not None and not self._reported_error:
                        print(f"[视频流读取失败] {error}", file=sys.stderr, flush=True)
                        self._reported_error = True
                    return False, None
                self._skipped += self._sequence - self._consumed - 1
                self._consumed = self._sequence
                encoded, received_at = self._latest
                self._frame_age_ms = (time.monotonic() - received_at) * 1000
            frame = self._decode(encoded)
            if frame is not None:
                with self._condition:
                    self._delivered += 1
                return True, frame
            if time.monotonic() >= deadline:
                return False, None

    def get_stats(self):
        """等待时间从本机收到完整 JPEG 算起，不是手机到屏幕的总延迟。"""
        with self._condition:
            return {
                "received": self._sequence,
                "delivered": self._delivered,
                "skipped": self._skipped,
                "frame_age_ms": self._frame_age_ms,
            }

    def release(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._reader is not None:
            # HTTP 读取自身有超时；先让接收线程退出，再关闭它正在使用的响应。
            self._reader.join(timeout=self.timeout + 0.5)
        if self.response is not None:
            self.response.close()
            self.response = None


def preview_raw(cap):
    """只显示解码后的原始画面，不运行识别，也不保存图像。"""
    print("原始画面预览：未运行手势模型；按 Q 或 ESC 退出。", flush=True)
    count = 0
    report_at = time.monotonic()
    report_count = 0
    received_count = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("[错误] 未能继续读取原始画面。", flush=True)
                return False
            if count == 0:
                print(f"原始帧：{frame.shape[1]}x{frame.shape[0]}，{frame.shape[2]} 通道。", flush=True)
            count += 1
            cv2.imshow("Raw Camera Preview", frame)
            now = time.monotonic()
            if now - report_at >= 2.0:
                elapsed = now - report_at
                detail = ""
                if hasattr(cap, "get_stats"):
                    stats = cap.get_stats()
                    detail = (
                        f"，接收 {(stats['received'] - received_count) / elapsed:.1f} fps"
                        f"，累计跳过旧帧 {stats['skipped']}"
                        f"，本机缓冲等待 {stats['frame_age_ms']:.1f} ms"
                    )
                    received_count = stats["received"]
                print(f"[预览] 显示 {(count - report_count) / elapsed:.1f} fps{detail}", flush=True)
                report_at, report_count = now, count
            if cv2.waitKey(1) & 0xff in (27, ord("q"), ord("Q")):
                return True
    finally:
        cv2.destroyAllWindows()
