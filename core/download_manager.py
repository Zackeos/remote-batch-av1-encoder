"""
Resumable HTTP(S) downloads from direct links, each on its own thread with live
progress, speed and ETA.

Downloads are written to "<name>.part" and renamed when complete. If a download
fails or the server restarts, retrying (or adding the same URL again) continues
from the existing .part file using an HTTP Range request.
"""

import os
import re
import time
import uuid
import threading
from typing import Dict, Any, List, Optional, Callable
from urllib.parse import urlparse, unquote

import requests

CHUNK_SIZE = 1024 * 1024
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
PROGRESS_INTERVAL_SEC = 0.4
MAX_CONCURRENT_DOWNLOADS = 3

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
FINISHED_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Reduces a server- or user-supplied name to a safe single path component."""
    name = unquote(name or "").replace("\\", "/").split("/")[-1]
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip().strip(".")
    return name[:255]


def filename_from_response(url: str, headers: Dict[str, str]) -> str:
    """Picks a filename from Content-Disposition (RFC 6266), falling back to the URL path."""
    cd = headers.get("content-disposition", "") or ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.IGNORECASE)
    if not m:
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE) or \
            re.search(r"filename\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        name = sanitize_filename(m.group(1).strip())
        if name:
            return name
    return sanitize_filename(urlparse(url).path)


def is_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def parse_content_range_total(value: str) -> int:
    """Returns the total size from a 'bytes start-end/total' header, or 0 if unknown."""
    m = re.match(r"bytes\s+\d+-\d+/(\d+)", value or "")
    return int(m.group(1)) if m else 0


class TransferTask:
    def __init__(self, source: str, destination: str, filename: Optional[str] = None):
        self.id = uuid.uuid4().hex[:8]
        self.source = source
        self.destination = destination
        self.filename = sanitize_filename(filename) if filename else ""
        self.status = STATUS_QUEUED
        self.total_bytes = 0
        self.transferred_bytes = 0
        self.resumed_from = 0
        self.progress = 0.0
        self.speed_mbs = 0.0
        self.eta_sec = 0
        self.error: Optional[str] = None
        self.output_path: Optional[str] = None
        self.on_complete: Optional[Callable[["TransferTask"], None]] = None
        self._cancel = threading.Event()

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename or self.source,
            "source": self.source,
            "destination": self.destination,
            "status": self.status,
            "total_bytes": self.total_bytes,
            "transferred_bytes": self.transferred_bytes,
            "resumed_from": self.resumed_from,
            "progress": self.progress,
            "speed_mbs": self.speed_mbs,
            "eta_sec": self.eta_sec,
            "error": self.error
        }

    def _update_rate(self, session_bytes: int, start_time: float):
        elapsed = max(0.1, time.time() - start_time)
        speed_bps = session_bytes / elapsed
        self.speed_mbs = round(speed_bps / (1024 * 1024), 1)
        if self.total_bytes > 0:
            self.progress = min(1.0, self.transferred_bytes / self.total_bytes)
            self.eta_sec = int(max(0, self.total_bytes - self.transferred_bytes) / max(1.0, speed_bps))


class DownloadManager:
    """Owns all download tasks. Downloads beyond MAX_CONCURRENT_DOWNLOADS wait their turn."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_DOWNLOADS):
        self.tasks: Dict[str, TransferTask] = {}
        self._lock = threading.RLock()
        self._slots = threading.Semaphore(max_concurrent)

    def add_download(
        self,
        url: str,
        dest_dir: str,
        filename: Optional[str] = None,
        on_complete: Optional[Callable[[TransferTask], None]] = None
    ) -> TransferTask:
        url = url.strip()
        if not is_http_url(url):
            raise ValueError(f"Not an http(s) URL: {url}")
        task = TransferTask(url, dest_dir, filename)
        task.on_complete = on_complete
        with self._lock:
            self.tasks[task.id] = task
        self._start(task, self._run_download)
        return task

    def retry(self, task_id: str) -> Optional[TransferTask]:
        """Restarts a failed download; it resumes from the .part file if one exists."""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.status != STATUS_FAILED:
                return task
            task.status = STATUS_QUEUED
            task.error = None
            task._cancel.clear()
        self._start(task, self._run_download)
        return task

    def cancel(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
            if task and not task.is_finished:
                task._cancel.set()
                task.status = STATUS_CANCELLED

    def clear_finished(self):
        with self._lock:
            for tid in [tid for tid, t in self.tasks.items() if t.is_finished]:
                del self.tasks[tid]

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self.tasks.values()]

    @staticmethod
    def _start(task: TransferTask, target: Callable[[TransferTask], None]):
        threading.Thread(target=target, args=(task,), daemon=True).start()

    def _run_download(self, task: TransferTask):
        with self._slots:
            if task._cancel.is_set():
                return
            task.status = STATUS_DOWNLOADING
            try:
                self._download(task)
            except Exception as e:
                if not task._cancel.is_set():
                    task.status = STATUS_FAILED
                    task.error = str(e)
                    task.speed_mbs = 0.0
                return

        if task.status == STATUS_COMPLETED and task.on_complete:
            try:
                task.on_complete(task)
            except Exception as e:
                task.error = f"Downloaded, but post-download step failed: {e}"

    def _download(self, task: TransferTask):
        os.makedirs(task.destination, exist_ok=True)
        timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)

        resp = requests.get(task.source, stream=True, timeout=timeout)
        resp.raise_for_status()
        if not task.filename:
            task.filename = filename_from_response(resp.url or task.source, resp.headers) or f"download_{task.id}"

        out_path = os.path.join(task.destination, task.filename)
        part_path = out_path + ".part"
        full_size = int(resp.headers.get("content-length") or 0)
        offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0

        if offset and full_size and offset > full_size:
            offset = 0
        if offset and full_size and offset == full_size:
            resp.close()
            task.total_bytes = task.transferred_bytes = full_size
        else:
            if offset:
                # Ask the server to continue where the previous attempt stopped.
                resp.close()
                resp = requests.get(task.source, stream=True, timeout=timeout, headers={"Range": f"bytes={offset}-"})
                resp.raise_for_status()
                if resp.status_code != 206:
                    offset = 0  # Server ignored the Range header; start over.

            if offset:
                task.resumed_from = offset
                task.total_bytes = parse_content_range_total(resp.headers.get("content-range", "")) or \
                    offset + int(resp.headers.get("content-length") or 0)
            else:
                task.total_bytes = int(resp.headers.get("content-length") or 0)
            task.transferred_bytes = offset
            self._write_stream(task, resp, part_path, append=bool(offset))

        if task._cancel.is_set():
            if os.path.exists(part_path):
                os.remove(part_path)
            return

        if task.total_bytes and os.path.getsize(part_path) < task.total_bytes:
            raise IOError("Connection closed before the download finished; retry to resume.")

        os.replace(part_path, out_path)
        task.output_path = out_path
        task.status = STATUS_COMPLETED
        task.progress = 1.0
        task.speed_mbs = 0.0
        task.eta_sec = 0

    @staticmethod
    def _write_stream(task: TransferTask, resp: requests.Response, part_path: str, append: bool):
        start_time = time.time()
        session_bytes = 0
        last_update = 0.0
        with resp, open(part_path, "ab" if append else "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if task._cancel.is_set():
                    return
                if not chunk:
                    continue
                f.write(chunk)
                session_bytes += len(chunk)
                task.transferred_bytes += len(chunk)
                now = time.time()
                if now - last_update > PROGRESS_INTERVAL_SEC:
                    last_update = now
                    task._update_rate(session_bytes, start_time)
