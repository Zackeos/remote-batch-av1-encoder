import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.download_manager import (
    DownloadManager, sanitize_filename, filename_from_response, is_http_url,
    parse_content_range_total, STATUS_COMPLETED, STATUS_FAILED
)

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB of non-repeating-ish data


class FileHandler(BaseHTTPRequestHandler):
    """Serves PAYLOAD at any path, honouring Range requests unless the server disables them."""

    def do_GET(self):
        support_ranges = self.server.support_ranges
        self.server.requests.append(self.headers.get("Range"))
        if self.path.startswith("/named"):
            disposition = 'attachment; filename="Named File.mkv"'
        else:
            disposition = None

        m = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
        if m and support_ranges:
            start = int(m.group(1))
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        if support_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
    httpd.support_ranges = True
    httpd.requests = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def url_for(httpd, path):
    return f"http://127.0.0.1:{httpd.server_address[1]}{path}"


def wait_finished(task, timeout=10):
    deadline = time.time() + timeout
    while not task.is_finished and time.time() < deadline:
        time.sleep(0.02)
    assert task.is_finished, f"task still {task.status}"


@pytest.mark.parametrize("raw, expected", [
    ("video.mkv", "video.mkv"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\evil.mkv", "evil.mkv"),
    ("My%20Show%20E01.mkv", "My Show E01.mkv"),
    ('bad:name?"<>|.mkv', "bad_name_____.mkv"),
    ("..", ""),
    ("", ""),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("url, headers, expected", [
    ("https://host/files/Show.E01.mkv?token=abc", {}, "Show.E01.mkv"),
    ("https://host/dl", {"content-disposition": 'attachment; filename="Movie (2020).mkv"'}, "Movie (2020).mkv"),
    ("https://host/dl", {"content-disposition": "attachment; filename=plain.mp4"}, "plain.mp4"),
    ("https://host/dl", {"content-disposition": "attachment; filename*=UTF-8''Caf%C3%A9.mkv"}, "Café.mkv"),
    ("https://host/dl", {"content-disposition": 'attachment; filename="../../escape.mkv"'}, "escape.mkv"),
])
def test_filename_from_response(url, headers, expected):
    assert filename_from_response(url, headers) == expected


@pytest.mark.parametrize("url, ok", [
    ("https://example.com/a.mkv", True),
    ("http://example.com/a.mkv", True),
    ("ftp://example.com/a.mkv", False),
    ("file:///etc/passwd", False),
    ("not a url", False),
])
def test_is_http_url(url, ok):
    assert is_http_url(url) is ok


def test_parse_content_range_total():
    assert parse_content_range_total("bytes 100-999/1000") == 1000
    assert parse_content_range_total("bytes 0-9/*") == 0
    assert parse_content_range_total("") == 0


def test_download_completes(tmp_path, server):
    done = []
    task = DownloadManager().add_download(url_for(server, "/files/video.mkv"), str(tmp_path), on_complete=done.append)
    wait_finished(task)

    assert task.status == STATUS_COMPLETED
    assert (tmp_path / "video.mkv").read_bytes() == PAYLOAD
    assert not (tmp_path / "video.mkv.part").exists()
    assert task.progress == 1.0
    assert done == [task]


def test_download_uses_content_disposition_name(tmp_path, server):
    task = DownloadManager().add_download(url_for(server, "/named"), str(tmp_path))
    wait_finished(task)
    assert (tmp_path / "Named File.mkv").exists()


def test_download_resumes_from_part_file(tmp_path, server):
    half = len(PAYLOAD) // 2
    (tmp_path / "video.mkv.part").write_bytes(PAYLOAD[:half])

    task = DownloadManager().add_download(url_for(server, "/video.mkv"), str(tmp_path))
    wait_finished(task)

    assert task.status == STATUS_COMPLETED
    assert task.resumed_from == half
    assert server.requests[-1] == f"bytes={half}-"
    assert (tmp_path / "video.mkv").read_bytes() == PAYLOAD


def test_download_restarts_when_server_ignores_range(tmp_path, server):
    server.support_ranges = False
    (tmp_path / "video.mkv.part").write_bytes(b"stale partial data")

    task = DownloadManager().add_download(url_for(server, "/video.mkv"), str(tmp_path))
    wait_finished(task)

    assert task.status == STATUS_COMPLETED
    assert task.resumed_from == 0
    assert (tmp_path / "video.mkv").read_bytes() == PAYLOAD


def test_complete_part_file_is_finalised_without_redownload(tmp_path, server):
    (tmp_path / "video.mkv.part").write_bytes(PAYLOAD)
    task = DownloadManager().add_download(url_for(server, "/video.mkv"), str(tmp_path))
    wait_finished(task)

    assert task.status == STATUS_COMPLETED
    assert server.requests == [None]
    assert (tmp_path / "video.mkv").read_bytes() == PAYLOAD


def test_failed_download_keeps_part_and_can_retry(tmp_path, server):
    manager = DownloadManager()
    dead_url = "http://127.0.0.1:9/video.mkv"  # discard port: connection refused
    task = manager.add_download(dead_url, str(tmp_path), filename="video.mkv")
    wait_finished(task)
    assert task.status == STATUS_FAILED
    assert task.error

    # Point the task at a working server and retry: it should resume from the existing .part.
    (tmp_path / "video.mkv.part").write_bytes(PAYLOAD[:1000])
    task.source = url_for(server, "/video.mkv")
    manager.retry(task.id)
    time.sleep(0.05)
    wait_finished(task)
    assert task.status == STATUS_COMPLETED
    assert task.resumed_from == 1000
    assert (tmp_path / "video.mkv").read_bytes() == PAYLOAD


def test_add_download_rejects_non_http(tmp_path):
    with pytest.raises(ValueError):
        DownloadManager().add_download("file:///etc/passwd", str(tmp_path))


def test_clear_finished_removes_only_finished(tmp_path, server):
    manager = DownloadManager()
    task = manager.add_download(url_for(server, "/video.mkv"), str(tmp_path))
    wait_finished(task)
    manager.clear_finished()
    assert manager.get_all_tasks() == []
    assert os.path.exists(tmp_path / "video.mkv")
