import fcntl
import hashlib
import importlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parents[1]
PREFIX = "steering_control.chat"
download = importlib.import_module(PREFIX + ".download")
deployment = importlib.import_module(PREFIX + ".deployment")


class Response(io.BytesIO):
    def __init__(self, data, *, status=200, headers=None, fail_after=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}
        self.fail_after = fail_after

    def read(self, size=-1):
        if self.fail_after is not None:
            if self.tell() >= self.fail_after:
                raise ConnectionResetError("模拟断网")
            size = min(size, self.fail_after - self.tell())
        return super().read(size)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.run = HERE / "data/tests" / uuid.uuid4().hex
        self.run.mkdir(parents=True, exist_ok=False)
        self.path = self.run / "synthetic.gguf"
        self.data = b"GGUFabcdefghijklmnop"

    def full(self):
        return Response(self.data, headers={"Content-Length": str(len(self.data))})

    def ranged(self, start, **kwargs):
        headers = {"Content-Range": f"bytes {start}-{len(self.data) - 1}/{len(self.data)}",
                   "Content-Length": str(len(self.data) - start)}
        return Response(self.data[start:], status=206, headers=headers, **kwargs)

    def test_initial_download(self):
        download.download_file("https://example.test/model", self.path, len(self.data), opener=lambda *a, **k: self.full())
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_existing_legacy_partial_resumes_without_duplicate_bytes(self):
        self.path.write_bytes(self.data[:10])
        requests = []

        def opener(request, **kwargs):
            requests.append(request.get_header("Range"))
            return self.ranged(7)

        download.download_file("https://example.test/model", self.path, len(self.data), opener=opener, overlap_size=3)
        self.assertEqual(requests, ["bytes=7-"])
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_network_cut_retries_from_saved_offset(self):
        requests, delays = [], []

        def opener(request, **kwargs):
            requests.append(request.get_header("Range"))
            if len(requests) == 1:
                return Response(self.data, headers={"Content-Length": str(len(self.data))}, fail_after=6)
            return self.ranged(4)

        download.download_file("https://example.test/model", self.path, len(self.data), opener=opener,
                               sleep=delays.append, chunk_size=4, overlap_size=2)
        self.assertEqual(requests, [None, "bytes=4-"])
        self.assertEqual(delays, [2])
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_restart_after_exhausted_retry_keeps_received_bytes(self):
        first = Response(self.data[:7], headers={"Content-Length": str(len(self.data))})
        with self.assertRaises(ConnectionError):
            download.download_file("https://example.test/model", self.path, len(self.data),
                                   opener=lambda *a, **k: first, retries=0, chunk_size=4)
        self.assertEqual(self.path.read_bytes(), self.data[:7])
        download.download_file("https://example.test/model", self.path, len(self.data),
                               opener=lambda *a, **k: self.ranged(4), overlap_size=3)
        self.assertEqual(self.path.read_bytes(), self.data)

    def test_ignored_range_never_appends_entire_file(self):
        original = self.data[:10]
        self.path.write_bytes(original)
        with self.assertRaises(download.RangeError):
            download.download_file("https://example.test/model", self.path, len(self.data),
                                   opener=lambda *a, **k: self.full(), overlap_size=3)
        self.assertEqual(self.path.read_bytes(), original)

    def test_inconsistent_range_total_length_or_encoding_does_not_append(self):
        for headers in [
            {"Content-Range": f"bytes 6-{len(self.data)-1}/{len(self.data)}"},
            {"Content-Range": f"bytes 7-{len(self.data)-1}/{len(self.data)+1}"},
            {"Content-Range": "invalid"},
            {"Content-Range": f"bytes 7-9/{len(self.data)}", "Content-Length": "3"},
            {"Content-Range": f"bytes 7-{len(self.data)-1}/{len(self.data)}", "Content-Length": "1"},
            {"Content-Encoding": "gzip"},
        ]:
            with self.subTest(headers=headers):
                # Each case gets its own fresh file; no old output is overwritten.
                target = self.run / (uuid.uuid4().hex + ".gguf")
                target.write_bytes(self.data[:10])
                response = Response(self.data[7:], status=206, headers=headers)
                with self.assertRaises(download.RangeError):
                    download.download_file("https://example.test/model", target, len(self.data),
                                           opener=lambda *a, **k: response, overlap_size=3)
                self.assertEqual(target.read_bytes(), self.data[:10])

    def test_mismatched_old_tail_does_not_append(self):
        self.path.write_bytes(b"bad-prefix")
        with self.assertRaisesRegex(download.RangeError, "末尾"):
            download.download_file("https://example.test/model", self.path, len(self.data),
                                   opener=lambda *a, **k: self.ranged(7), overlap_size=3)
        self.assertEqual(self.path.read_bytes(), b"bad-prefix")

    def test_second_downloader_is_rejected(self):
        self.path.write_bytes(self.data[:10])
        with self.path.open("ab") as writer:
            fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(download.RangeError, "另一个下载"):
                download.download_file("https://example.test/model", self.path, len(self.data),
                                       opener=lambda *a, **k: self.fail("不应开始网络请求"))
        self.assertEqual(self.path.read_bytes(), self.data[:10])

    def test_incomplete_read_partial_is_preserved(self):
        from http.client import IncompleteRead

        class Cut(Response):
            def read(self, size=-1):
                if self.tell() == 0:
                    super().read(6)
                    raise IncompleteRead(self.getvalue()[:6], len(self.getvalue()) - 6)
                return b""

        response = Cut(self.data, headers={"Content-Length": str(len(self.data))})
        with self.assertRaises(IncompleteRead):
            download.download_file("https://example.test/model", self.path, len(self.data),
                                   opener=lambda *a, **k: response, retries=0)
        self.assertEqual(self.path.read_bytes(), self.data[:6])

    def test_full_file_does_not_download_again(self):
        self.path.write_bytes(self.data)
        download.download_file("https://example.test/model", self.path, len(self.data),
                               opener=lambda *a, **k: self.fail("完整文件不应重复下载"))

    def test_prepare_adopts_old_directory_and_only_marks_ready_after_sha(self):
        old = self.run / "models/old_version/input_gguf" / deployment.MODEL_FILE
        old.parent.mkdir(parents=True)
        old.write_bytes(self.data[:10])

        def resume(url, path, expected_size):
            return download.download_file(url, path, expected_size, overlap_size=3,
                                          opener=lambda *a, **k: self.ranged(7))

        with patch.object(deployment, "HERE", self.run), patch.object(deployment, "MODEL_SIZE", len(self.data)), patch.object(deployment, "MODEL_SHA256", hashlib.sha256(self.data).hexdigest()), patch.object(deployment, "download_file", side_effect=resume):
            result = deployment.prepare_model()
        self.assertEqual(result, old)
        self.assertEqual(old.read_bytes(), self.data)
        self.assertTrue((old.parent.parent / "output_json/source.json").is_file())

    def test_bad_sha_never_marks_file_ready(self):
        old = self.run / "models/old_version/input_gguf" / deployment.MODEL_FILE
        old.parent.mkdir(parents=True)
        old.write_bytes(self.data[:10])

        def resume(url, path, expected_size):
            return download.download_file(url, path, expected_size, overlap_size=3,
                                          opener=lambda *a, **k: self.ranged(7))

        with patch.object(deployment, "HERE", self.run), patch.object(deployment, "MODEL_SIZE", len(self.data)), patch.object(deployment, "MODEL_SHA256", "0" * 64), patch.object(deployment, "download_file", side_effect=resume):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                deployment.prepare_model()
        self.assertFalse((old.parent.parent / "output_json/source.json").exists())
        self.assertEqual(old.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
