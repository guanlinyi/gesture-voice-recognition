"""Append-only HTTP range download. Complete files still require caller SHA256."""
from __future__ import annotations

import errno
import fcntl
from http.client import IncompleteRead
import os
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RangeError(RuntimeError):
    """A response cannot safely be appended; retain the file and stop."""


def _transient(error):
    if isinstance(error, HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (PermissionError, ssl.SSLCertVerificationError)):
        return False
    return (isinstance(reason, (TimeoutError, ConnectionError, IncompleteRead, ssl.SSLError))
            or isinstance(error, URLError)
            or getattr(reason, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT))


def download_file(url, path: Path, expected_size: int, *, retries=3, opener=urlopen,
                  sleep=time.sleep, chunk_size=1024 * 1024, overlap_size=64 * 1024):
    """Resume existing bytes, verify overlap/framing, and retry transient errors.

    Uses only append writes. A server ignoring a nonzero Range, mismatched overlap
    or changed total size is rejected without truncating any existing bytes.
    The advisory lock coordinates new downloaders, not already-running old code.
    """
    path = Path(path)
    failures = 0
    with path.open("a+b") as output:
        try:
            fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RangeError("已有另一个下载进程在使用该文件，请勿同时运行") from exc
        last_report = time.monotonic()
        while True:
            offset = os.fstat(output.fileno()).st_size
            if offset == expected_size:
                return path
            if offset > expected_size:
                raise RangeError("已有文件比目标模型更大，已保留，不能续传")
            start = max(0, offset - overlap_size)
            overlap = offset - start
            output.seek(start)
            old_tail = output.read(overlap)
            headers = {"Accept-Encoding": "identity", "User-Agent": "OrinSpeechResearch/1.0"}
            if offset:
                headers["Range"] = f"bytes={start}-"
                print(f"从已有 {offset / 1024**2:.1f} MiB 继续下载（核对末尾 {overlap} 字节）", flush=True)
            request = Request(url, headers=headers)
            try:
                with opener(request, timeout=60) as response:
                    status = response.status
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise RangeError("服务器返回压缩传输，不能核对文件字节位置")
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match:
                            raise RangeError("服务器没有返回有效的 Content-Range，原文件保留")
                        first, end, total = map(int, match.groups())
                        if first != start or total != expected_size or not first <= end < total or end + 1 <= offset:
                            raise RangeError("服务器续传位置或模型大小不匹配，原文件保留")
                    elif status == 200 and start == 0:
                        end = expected_size - 1
                    else:
                        raise RangeError("服务器没有支持本次 Range 续传，原文件保留；请稍后重试")
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) != end - start + 1:
                        raise RangeError("服务器响应长度与目标范围不一致，原文件保留")
                    received_tail = bytearray()
                    while len(received_tail) < overlap:
                        data = response.read(overlap - len(received_tail))
                        if not data:
                            raise ConnectionError("核对旧数据时连接中断")
                        received_tail.extend(data)
                    if received_tail != old_tail:
                        raise RangeError("旧文件末尾与服务器数据不一致，未追加任何字节；请检查下载来源")
                    if os.fstat(output.fileno()).st_size != offset:
                        raise RangeError("文件正在被其他进程修改，请先等原下载进程退出")
                    position = offset

                    def append(data):
                        nonlocal position, last_report
                        if not data:
                            return
                        if position + len(data) > end + 1:
                            raise RangeError("服务器返回超出声明范围的数据，已停止追加")
                        if os.fstat(output.fileno()).st_size != position:
                            raise RangeError("文件正在被其他进程修改，请勿同时运行下载")
                        output.write(data)
                        output.flush()
                        position += len(data)
                        if time.monotonic() - last_report >= 10:
                            print(f"已下载 {position / 1024**2:.0f}/{expected_size / 1024**2:.0f} MiB ({position / expected_size:.1%})", flush=True)
                            last_report = time.monotonic()

                    try:
                        while data := response.read(chunk_size):
                            append(data)
                    except IncompleteRead as exc:
                        # HTTP chunked transfers can include valid last bytes in the exception.
                        append(exc.partial)
                        raise
                    if position != end + 1:
                        raise ConnectionError("下载连接提前结束，已保存收到的部分")
                failures = 0
            except (OSError, IncompleteRead) as exc:
                if not _transient(exc) or failures >= retries:
                    raise
                failures += 1
                delay = min(2 ** failures, 8)
                print(f"连接中断，{delay} 秒后自动续传（重试 {failures}/{retries}）；已下载部分保留", flush=True)
                sleep(delay)
