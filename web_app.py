"""
FastAPI web dashboard: folder scanning, encode queue control, URL downloads,
auto-tune, and live telemetry over a WebSocket.
"""

import os
import sys
import shutil
import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional

import psutil
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.constants import (
    APP_TITLE, APP_VERSION, WORKER_OPTIONS, VIDEO_CQ_OPTIONS, SVT_PRESET_OPTIONS,
    FILM_GRAIN_OPTIONS, OPUS_BITRATE_OPTIONS, COLOR_FORMAT_OPTIONS, RESOLUTION_OPTIONS,
    NORM_MODE_OPTIONS, AUDIO_TRACK_OPTIONS, RATE_CONTROL_OPTIONS, AUDIO_CHANNELS_OPTIONS,
    RESOLUTION_SOURCE, RATE_CONTROL_CQ, COLOR_FORMAT_AUTO, normalize_language
)
from core.job import Job, STATUS_PENDING, STATUS_ENCODING, STATUS_COMPLETED
from core.queue_manager import QueueManager
from core.parser import scan_video_files, is_video_file
from core.probe import probe_file_details
from core.autotune import run_autotune_benchmark
from core.download_manager import DownloadManager, sanitize_filename, is_http_url
from core.settings import load_settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(BASE_DIR, "templates", "index.html")

SETTINGS = load_settings()
INPUT_DIR = SETTINGS["input_dir"]
OUTPUT_DIR = SETTINGS["output_dir"]
# Encoded outputs this small are treated as incomplete leftovers.
MIN_ENCODED_MB = 5.0

qm = QueueManager()
dl_manager = DownloadManager()

active_connections: List[WebSocket] = []
main_loop: Optional[asyncio.AbstractEventLoop] = None

autotune_state: Dict[str, Any] = {"running": False}
autotune_cancel = threading.Event()


def resolve_inside(root: str, path: str) -> str:
    """Resolves path (absolute or relative to root) and rejects anything outside root."""
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, path))
    if os.path.commonpath([root_abs, target]) != root_abs:
        raise HTTPException(status_code=400, detail=f"Path is outside {root_abs}")
    return target


def output_dir_for(folder: str) -> str:
    """Mirrors a folder under INPUT_DIR to the matching folder under OUTPUT_DIR."""
    rel = os.path.relpath(folder, INPUT_DIR)
    return OUTPUT_DIR if rel == "." else os.path.join(OUTPUT_DIR, rel)


def encoded_size_mb(source_path: str, encoded_dir: str) -> float:
    stem = os.path.splitext(os.path.basename(source_path))[0]
    enc_path = os.path.join(encoded_dir, f"{stem}.mkv")
    if os.path.exists(enc_path):
        size = os.path.getsize(enc_path) / (1024 * 1024)
        if size > MIN_ENCODED_MB:
            return size
    return 0.0


def _visible_files(folder: str) -> List[str]:
    return [f for f in sorted(os.listdir(folder)) if not f.startswith(".") and os.path.isfile(os.path.join(folder, f))]


def jobs_payload() -> List[Dict[str, Any]]:
    return [j.to_dict() for j in qm.jobs]


async def broadcast_ws(message: dict):
    for ws in list(active_connections):
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)


def broadcast_threadsafe(message: dict):
    """Sends a WebSocket message from a worker thread via the server's event loop."""
    if main_loop and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_ws(message), main_loop)


def on_queue_event(event_type: str, data: Any):
    msg: Dict[str, Any] = {"event": event_type}
    if event_type == "job_status":
        msg["job"] = data.to_dict()
    elif event_type in ("job_added", "jobs_added", "job_removed", "queue_updated"):
        msg["jobs"] = jobs_payload()
    elif event_type == "queue_status":
        msg["status"] = data
    elif event_type == "log":
        msg["log"] = str(data)
    elif event_type == "batch_completed":
        msg["completed_count"] = data["completed_count"]
        msg["time_str"] = data["time_str"]
    broadcast_threadsafe(msg)


qm.add_listener(on_queue_event)


async def system_telemetry_loop():
    while True:
        try:
            mem = psutil.virtual_memory()
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            disk = psutil.disk_usage(OUTPUT_DIR)
            jobs = list(qm.jobs)
            await broadcast_ws({
                "event": "telemetry",
                "cpu_percent": psutil.cpu_percent(interval=None),
                "cpu_count": psutil.cpu_count(logical=True),
                "ram_percent": mem.percent,
                "ram_used_gb": round((mem.total - mem.available) / (1024**3), 1),
                "ram_total_gb": round(mem.total / (1024**3), 1),
                "disk_free_gb": round(disk.free / (1024**3), 1),
                "downloads": dl_manager.get_all_tasks(),
                "queue": {
                    "total": len(jobs),
                    "pending": sum(1 for j in jobs if j.status == STATUS_PENDING),
                    "active": sum(1 for j in jobs if j.status == STATUS_ENCODING),
                    "completed": sum(1 for j in jobs if j.status == STATUS_COMPLETED),
                    "is_running": qm.is_running,
                    "is_paused": qm.is_paused
                }
            })
        except Exception:
            pass
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    telemetry_task = asyncio.create_task(system_telemetry_loop())
    yield
    telemetry_task.cancel()


app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        await websocket.send_json({
            "event": "init",
            "jobs": jobs_payload(),
            "autotune": autotune_state,
            "paths": {"input_dir": INPUT_DIR, "output_dir": OUTPUT_DIR},
            "defaults": {"audio_language": SETTINGS["audio_language"]},
            "options": {
                "workers": WORKER_OPTIONS,
                "cq": VIDEO_CQ_OPTIONS,
                "preset": SVT_PRESET_OPTIONS,
                "grain": FILM_GRAIN_OPTIONS,
                "bitrate": OPUS_BITRATE_OPTIONS,
                "channels": AUDIO_CHANNELS_OPTIONS,
                "color": COLOR_FORMAT_OPTIONS,
                "rate_control": RATE_CONTROL_OPTIONS,
                "norm": NORM_MODE_OPTIONS,
                "audio_tracks": AUDIO_TRACK_OPTIONS,
                "resolution": RESOLUTION_OPTIONS
            }
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)


@app.get("/")
def serve_index():
    return FileResponse(TEMPLATE_FILE)


# --- Library -----------------------------------------------------------------

class ScanRequest(BaseModel):
    path: str = ""
    recursive: bool = False


@app.post("/api/scan")
def scan_directory(req: ScanRequest):
    target_path = resolve_inside(INPUT_DIR, req.path)
    os.makedirs(target_path, exist_ok=True)
    encoded_dir = output_dir_for(target_path)

    files = []
    sample_details = None
    for idx, (rel_dir, s_num, ep_num, f_path, label) in enumerate(scan_video_files(target_path, recursive=req.recursive)):
        size_mb = round(os.path.getsize(f_path) / (1024 * 1024), 1)
        enc_mb = encoded_size_mb(f_path, os.path.join(encoded_dir, rel_dir))
        info = {
            "index": idx,
            "path": f_path,
            "filename": os.path.basename(f_path),
            "label": label,
            "season": s_num,
            "episode": ep_num,
            "size_mb": size_mb,
            "already_encoded": enc_mb > 0,
            "encoded_size_mb": round(enc_mb, 1),
            "compression_pct": round((1.0 - enc_mb / size_mb) * 100, 1) if enc_mb and size_mb else 0.0
        }
        if idx == 0:
            sample_details = probe_file_details(f_path)
        files.append(info)

    return {
        "count": len(files),
        "files": files,
        "folder_path": target_path,
        "suggested_output_dir": encoded_dir,
        "already_encoded_count": sum(1 for f in files if f["already_encoded"]),
        "new_count": sum(1 for f in files if not f["already_encoded"]),
        "sample_details": sample_details
    }


@app.get("/api/raw/folders")
def list_raw_folders():
    os.makedirs(INPUT_DIR, exist_ok=True)

    def summary(name: str, path: str) -> Dict[str, Any]:
        files = _visible_files(path)
        size_mb = sum(os.path.getsize(os.path.join(path, f)) for f in files) / (1024 * 1024)
        return {"name": name, "path": path, "file_count": len(files), "size_gb": round(size_mb / 1024, 2)}

    folders = [summary("(input root)", INPUT_DIR)]
    for item in sorted(os.listdir(INPUT_DIR)):
        p = os.path.join(INPUT_DIR, item)
        if os.path.isdir(p) and not item.startswith("."):
            folders.append(summary(item, p))
    return {"folders": folders}


class CreateFolderRequest(BaseModel):
    folder_name: str


@app.post("/api/raw/folders/create")
def create_raw_folder(req: CreateFolderRequest):
    name = sanitize_filename(req.folder_name)
    if not name:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    target = resolve_inside(INPUT_DIR, name)
    os.makedirs(target, exist_ok=True)
    return {"status": "success", "folder": name, "path": target}


class MoveFilesRequest(BaseModel):
    paths: List[str]
    target_folder: str = ""


@app.post("/api/raw/move")
def move_raw_files(req: MoveFilesRequest):
    target_dir = resolve_inside(INPUT_DIR, sanitize_filename(req.target_folder))
    os.makedirs(target_dir, exist_ok=True)
    moved = 0
    for p in req.paths:
        src = resolve_inside(INPUT_DIR, p)
        if os.path.isfile(src):
            try:
                shutil.move(src, os.path.join(target_dir, os.path.basename(src)))
                moved += 1
            except OSError:
                pass
    return {"status": "success", "moved_count": moved}


@app.get("/api/raw/list")
def list_raw_files():
    os.makedirs(INPUT_DIR, exist_ok=True)
    files = []
    for f in _visible_files(INPUT_DIR):
        p = os.path.join(INPUT_DIR, f)
        enc_mb = encoded_size_mb(p, OUTPUT_DIR)
        files.append({
            "filename": f,
            "path": p,
            "size_mb": round(os.path.getsize(p) / (1024 * 1024), 1),
            "is_encoded": enc_mb > 0,
            "encoded_size_mb": round(enc_mb, 1)
        })
    return {"files": files, "total_size_gb": round(sum(f["size_mb"] for f in files) / 1024, 2)}


class DeleteFilesRequest(BaseModel):
    paths: List[str]


@app.post("/api/raw/delete")
def delete_raw_files(req: DeleteFilesRequest):
    deleted = 0
    for p in req.paths:
        target = resolve_inside(INPUT_DIR, p)
        if os.path.isfile(target):
            try:
                os.remove(target)
                deleted += 1
            except OSError:
                pass
    return {"status": "success", "deleted_count": deleted}


@app.post("/api/raw/clean-encoded")
def clean_encoded_raw_files():
    """Deletes source files in the input root that already have an encoded output."""
    deleted = 0
    freed_mb = 0.0
    for f in _visible_files(INPUT_DIR):
        p = os.path.join(INPUT_DIR, f)
        if encoded_size_mb(p, OUTPUT_DIR) > 0:
            size = os.path.getsize(p) / (1024 * 1024)
            try:
                os.remove(p)
                deleted += 1
                freed_mb += size
            except OSError:
                pass
    return {"status": "success", "deleted_count": deleted, "freed_mb": round(freed_mb, 1)}


# --- Encode queue ------------------------------------------------------------

class EncodeSettings(BaseModel):
    video_cq: str = "22"
    svt_preset: str = "6"
    film_grain: str = FILM_GRAIN_OPTIONS[0]
    bit_depth: str = COLOR_FORMAT_AUTO
    resolution: str = RESOLUTION_SOURCE
    audio_bitrate: str = "96"
    audio_channels: str = AUDIO_CHANNELS_OPTIONS[0]
    norm_mode: str = NORM_MODE_OPTIONS[0]
    audio_tracks: str = AUDIO_TRACK_OPTIONS[0]
    audio_language: str = SETTINGS["audio_language"]
    smart_sub_matching: bool = True
    selected_subs: List[int] = []
    custom_sub_titles: Dict[int, str] = {}
    rate_control_mode: str = RATE_CONTROL_CQ
    target_size_mb: Optional[float] = None
    workers: int = 8


class AddQueueRequest(EncodeSettings):
    paths: List[str]
    output_dir: str = ""


def build_job(source_path: str, output_dir: str, s: EncodeSettings) -> Job:
    return Job(
        source_path=source_path,
        output_dir=output_dir,
        video_cq=s.video_cq,
        svt_preset=s.svt_preset,
        bit_depth=s.bit_depth,
        film_grain=s.film_grain,
        audio_bitrate=s.audio_bitrate,
        audio_channels=s.audio_channels,
        norm_mode=s.norm_mode,
        audio_tracks=s.audio_tracks,
        audio_language=normalize_language(s.audio_language),
        smart_sub_matching=s.smart_sub_matching,
        selected_subs=list(s.selected_subs),
        custom_sub_titles=dict(s.custom_sub_titles),
        resolution=s.resolution,
        rate_control_mode=s.rate_control_mode,
        target_size_mb=s.target_size_mb,
        worker_threads=max(1, s.workers)
    )


@app.post("/api/queue/add")
def add_to_queue(req: AddQueueRequest):
    output_dir = resolve_inside(OUTPUT_DIR, req.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    qm.max_workers = max(1, req.workers)

    new_jobs = []
    for p in req.paths:
        source = resolve_inside(INPUT_DIR, p)
        if os.path.isfile(source):
            new_jobs.append(build_job(source, output_dir, req))
    if new_jobs:
        qm.add_jobs(new_jobs)

    return {
        "status": "success",
        "added_count": len(new_jobs),
        "job_ids": [j.id for j in new_jobs],
        "jobs": jobs_payload()
    }


@app.get("/api/queue")
def get_queue():
    return {"jobs": jobs_payload(), "is_running": qm.is_running, "is_paused": qm.is_paused}


@app.post("/api/queue/start")
def start_queue():
    qm.start_queue()
    return {"status": "success", "is_running": qm.is_running, "is_paused": qm.is_paused, "jobs": jobs_payload()}


@app.post("/api/queue/pause")
def pause_queue():
    qm.pause_queue()
    return {"status": "success", "is_paused": qm.is_paused, "jobs": jobs_payload()}


@app.post("/api/queue/cancel_all")
def cancel_all_queue():
    qm.cancel_all()
    return {"status": "success", "jobs": jobs_payload()}


@app.post("/api/queue/clear_completed")
def clear_completed_queue():
    qm.clear_completed()
    return {"status": "success", "jobs": jobs_payload()}


@app.post("/api/queue/remove/{job_id}")
def remove_queue_job(job_id: str):
    qm.remove_job(job_id)
    return {"status": "success", "jobs": jobs_payload()}


@app.post("/api/queue/move/{job_id}/{direction}")
def move_queue_job(job_id: str, direction: str):
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")
    qm.move_job(job_id, direction)
    return {"status": "success", "jobs": jobs_payload()}


@app.post("/api/queue/retry/{job_id}")
def retry_queue_job(job_id: str):
    job = qm.retry_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "success", "job": job.to_dict()}


# --- URL downloads -----------------------------------------------------------

class DownloadRequest(BaseModel):
    urls: List[str]
    subfolder: str = ""
    add_to_queue: bool = False
    settings: EncodeSettings = EncodeSettings()


@app.post("/api/downloads")
def start_downloads(req: DownloadRequest):
    urls = [u.strip() for u in req.urls if u.strip() and not u.strip().startswith("#")]
    invalid = [u for u in urls if not is_http_url(u)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Not a valid http(s) link: {invalid[0]}")
    if not urls:
        raise HTTPException(status_code=400, detail="No links provided")

    subfolder = sanitize_filename(req.subfolder)
    dest_dir = resolve_inside(INPUT_DIR, subfolder)
    out_dir = output_dir_for(dest_dir)
    settings = req.settings

    def queue_finished_download(task):
        if is_video_file(task.output_path):
            qm.max_workers = max(1, settings.workers)
            qm.add_job(build_job(task.output_path, out_dir, settings))

    on_complete = queue_finished_download if req.add_to_queue else None
    tasks = [dl_manager.add_download(u, dest_dir, on_complete=on_complete) for u in urls]
    return {"status": "started", "tasks": [t.to_dict() for t in tasks]}


@app.get("/api/downloads")
def get_downloads():
    return {"downloads": dl_manager.get_all_tasks()}


@app.post("/api/downloads/{task_id}/cancel")
def cancel_download(task_id: str):
    dl_manager.cancel(task_id)
    return {"status": "success"}


@app.post("/api/downloads/{task_id}/retry")
def retry_download(task_id: str):
    task = dl_manager.retry(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Download not found")
    return {"status": "success", "task": task.to_dict()}


@app.post("/api/downloads/clear")
def clear_downloads():
    dl_manager.clear_finished()
    return {"status": "success"}


# --- Auto-tune ---------------------------------------------------------------

class AutoTuneRequest(BaseModel):
    path: str
    total_files: int = 1
    resolution: str = RESOLUTION_SOURCE
    bit_depth: str = COLOR_FORMAT_AUTO


@app.post("/api/autotune")
def start_autotune(req: AutoTuneRequest):
    global autotune_state
    if autotune_state.get("running"):
        raise HTTPException(status_code=409, detail="Auto-tune is already running")
    source = resolve_inside(INPUT_DIR, req.path)
    if not os.path.isfile(source):
        raise HTTPException(status_code=404, detail="File not found")

    autotune_cancel.clear()
    autotune_state = {"running": True, "file": os.path.basename(source), "progress": 0.0, "status": "Starting..."}
    broadcast_threadsafe({"event": "autotune", "state": autotune_state})

    def push(**changes):
        autotune_state.update(changes)
        broadcast_threadsafe({"event": "autotune", "state": autotune_state})

    def worker():
        details = probe_file_details(source)
        try:
            results, best, _ = run_autotune_benchmark(
                ep_path=source,
                output_dir=output_dir_for(os.path.dirname(source)),
                is_10bit="8-bit" not in req.bit_depth,
                total_files=max(1, req.total_files),
                file_duration=details.get("duration", 0.0),
                resolution=req.resolution,
                status_callback=lambda msg: push(status=msg),
                log_callback=lambda txt: on_queue_event("log", txt),
                progress_callback=lambda pct: push(progress=pct),
                cancel_check=autotune_cancel.is_set
            )
            if autotune_cancel.is_set():
                push(running=False, status="Canceled")
            elif not results:
                push(running=False, status="Failed", error="No profile produced a result. Check that FFmpeg has libsvtav1 and libvmaf.")
            else:
                push(running=False, status="Done", progress=1.0, results=results, best=best)
        except Exception as e:
            push(running=False, status="Failed", error=str(e))

    threading.Thread(target=worker, daemon=True).start()
    return {"status": "started"}


@app.post("/api/autotune/cancel")
def cancel_autotune():
    autotune_cancel.set()
    return {"status": "success"}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} web dashboard")
    parser.add_argument("--host", default=os.environ.get("AV1_HOST", "127.0.0.1"), help="Address to bind to (use 0.0.0.0 to expose on the network)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AV1_PORT", "8080")), help="Port to listen on")
    args = parser.parse_args()

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"{APP_TITLE} {APP_VERSION}")
    print(f"Input folder:  {INPUT_DIR}")
    print(f"Output folder: {OUTPUT_DIR}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
