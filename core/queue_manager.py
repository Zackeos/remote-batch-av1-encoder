"""
Thread-safe background queue manager.

Scheduling:
- Several pending jobs: a worker pool encodes multiple files in parallel.
- A single pending job with more than one worker: the file is split into temporal
  chunks that are encoded in parallel and joined losslessly (similar to Av1an).

Supports pause/resume, cancellation, and pushes progress events to listeners.
"""

import os
import time
import queue
import shutil
import threading
import subprocess
from typing import List, Dict, Any, Optional, Callable, Tuple

from .constants import (
    RATE_CONTROL_SIZE, AUDIO_TRACKS_ALL, AUDIO_TRACKS_FIRST,
    AUDIO_TRACKS_PREFERRED_AND_ORIGINAL, AUDIO_TRACKS_ORIGINAL_AND_PREFERRED, normalize_language
)
from .job import (
    Job, STATUS_PENDING, STATUS_ENCODING,
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED, STATUS_SKIPPED
)
from .probe import probe_file_details, measure_audio_loudness
from .encoder import (
    build_ffmpeg_chunk_encode_cmd, build_ffmpeg_audio_encode_cmd,
    build_mkvmerge_chunk_concat_cmd, build_mkvmerge_final_mux_cmd, build_audio_filter,
    calculate_target_bitrate_k, keeps_surround, norm_target_lufs, parse_ffmpeg_time,
    TIME_PATTERN, SPEED_PATTERN, FPS_PATTERN
)
from .process_utils import format_time_hms, suspend_process, resume_process, kill_process_tree

# Files shorter than this are not worth splitting into chunks.
MIN_CHUNKING_DURATION_SEC = 120.0
# Existing outputs smaller than this are treated as incomplete and re-encoded.
MIN_VALID_OUTPUT_MB = 10.0
# Used to estimate FPS from FFmpeg's speed multiplier when it does not report fps.
FALLBACK_FRAME_RATE = 23.976

def select_audio_streams(audio_tracks: str, audio_language: str, audio_streams: List[Dict[str, Any]]) -> Tuple[List[dict], int]:
    """
    Picks the audio streams to encode. Returns (streams in output order, index of the
    default track). The "original" track is the first audio stream in the source; when
    no track matches the preferred language, the original is used instead.
    """
    if not audio_streams:
        return [], 0

    preferred_lang = normalize_language(audio_language)
    original = audio_streams[0]
    preferred = next((a for a in audio_streams if normalize_language(a.get('language')) == preferred_lang), None)

    if audio_tracks == AUDIO_TRACKS_ALL:
        return list(audio_streams), audio_streams.index(preferred) if preferred else 0
    if audio_tracks == AUDIO_TRACKS_FIRST:
        return [original], 0
    if audio_tracks in (AUDIO_TRACKS_PREFERRED_AND_ORIGINAL, AUDIO_TRACKS_ORIGINAL_AND_PREFERRED):
        if preferred is None or preferred is original:
            return [original], 0
        if audio_tracks == AUDIO_TRACKS_PREFERRED_AND_ORIGINAL:
            return [preferred, original], 0
        return [original, preferred], 0
    return [preferred or original], 0


class QueueManager:
    """Singleton queue of encoding jobs, processed on background threads."""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(QueueManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self.jobs: List[Job] = []
        self.is_running: bool = False
        self.is_paused: bool = False
        self.cancel_requested: bool = False
        self.max_workers: int = 0

        self._lock = threading.RLock()
        self._worker_thread: Optional[threading.Thread] = None
        self.active_processes: Dict[str, subprocess.Popen] = {}
        self.listeners: List[Callable[[str, Any], None]] = []

    def add_listener(self, callback: Callable[[str, Any], None]):
        with self._lock:
            if callback not in self.listeners:
                self.listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, Any], None]):
        with self._lock:
            if callback in self.listeners:
                self.listeners.remove(callback)

    def _emit(self, event_type: str, data: Any = None):
        with self._lock:
            callbacks = list(self.listeners)
        for cb in callbacks:
            try:
                cb(event_type, data)
            except Exception:
                pass

    def add_job(self, job: Job):
        with self._lock:
            self.jobs.append(job)
        self._emit("job_added", job)
        self._emit("queue_updated", self.jobs)

    def add_jobs(self, jobs: List[Job]):
        with self._lock:
            self.jobs.extend(jobs)
        self._emit("jobs_added", jobs)
        self._emit("queue_updated", self.jobs)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def remove_job(self, job_id: str) -> bool:
        """Removes a job and kills any FFmpeg processes (including chunks) it owns."""
        with self._lock:
            target = next((j for j in self.jobs if j.id == job_id), None)
            if not target:
                return False

            for k, proc in list(self.active_processes.items()):
                if k == job_id or k.startswith(f"{job_id}_"):
                    kill_process_tree(proc.pid)
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    self.active_processes.pop(k, None)

            self.jobs.remove(target)
        self._emit("job_removed", job_id)
        self._emit("queue_updated", self.jobs)
        return True

    def move_job(self, job_id: str, direction: str) -> bool:
        """Moves a job 'up' or 'down' one place in the queue."""
        with self._lock:
            idx = next((i for i, j in enumerate(self.jobs) if j.id == job_id), None)
            if idx is None:
                return False
            if direction == "up" and idx > 0:
                self.jobs[idx], self.jobs[idx - 1] = self.jobs[idx - 1], self.jobs[idx]
            elif direction == "down" and idx < len(self.jobs) - 1:
                self.jobs[idx], self.jobs[idx + 1] = self.jobs[idx + 1], self.jobs[idx]
            else:
                return False
        self._emit("queue_updated", self.jobs)
        return True

    def retry_job(self, job_id: str) -> Optional[Job]:
        """Resets a finished job to pending and starts the queue if it is idle."""
        with self._lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if not job or job.status == STATUS_ENCODING:
                return job
            job.status = STATUS_PENDING
            job.status_text = "Queued"
            job.error_message = ""
            job.progress = 0.0
        self._emit("job_status", job)
        self._emit("queue_updated", self.jobs)
        if not self.is_running:
            self.start_queue()
        return job

    def clear_completed(self):
        """Removes completed, canceled, skipped and failed jobs."""
        with self._lock:
            self.jobs = [j for j in self.jobs if not j.is_finished]
        self._emit("queue_updated", self.jobs)

    def clear_all(self):
        self.cancel_all()
        with self._lock:
            self.jobs.clear()
        self._emit("queue_updated", self.jobs)
        self._emit("log", "[Queue] Cleared all jobs from queue.\n")

    def start_queue(self):
        with self._lock:
            if self.is_running:
                if self.is_paused:
                    self.resume_queue()
                return
            self.is_running = True
            self.is_paused = False
            self.cancel_requested = False
            self._worker_thread = threading.Thread(target=self._queue_worker_loop, daemon=True)
            self._worker_thread.start()
        self._emit("queue_status", {"is_running": True, "is_paused": False})

    def pause_queue(self):
        """Suspends all running FFmpeg processes."""
        with self._lock:
            if not self.is_running or self.is_paused:
                return
            self.is_paused = True
            for proc in list(self.active_processes.values()):
                suspend_process(proc.pid, trim_ram=True)
        self._emit("queue_status", {"is_running": True, "is_paused": True})
        self._emit("log", "[Queue] Paused. Encoder processes suspended.\n")

    def resume_queue(self):
        with self._lock:
            if not self.is_running or not self.is_paused:
                return
            self.is_paused = False
            for proc in list(self.active_processes.values()):
                resume_process(proc.pid)
        self._emit("queue_status", {"is_running": True, "is_paused": False})
        self._emit("log", "[Queue] Resumed.\n")

    def cancel_all(self):
        """Kills running encodes and marks all active and pending jobs as canceled."""
        with self._lock:
            self.cancel_requested = True
            self.is_running = False
            self.is_paused = False
            for proc in list(self.active_processes.values()):
                kill_process_tree(proc.pid)
                try:
                    proc.terminate()
                except Exception:
                    pass
            self.active_processes.clear()
            for j in self.jobs:
                if j.status in (STATUS_ENCODING, STATUS_PENDING):
                    j.status = STATUS_CANCELED
                    j.status_text = "Canceled"
        self._emit("queue_status", {"is_running": False, "is_paused": False})
        self._emit("queue_updated", self.jobs)
        self._emit("log", "\n[Queue] Canceled all active and pending jobs.\n")

    def _wait_while_paused(self):
        while self.is_paused and not self.cancel_requested:
            time.sleep(0.2)

    def _run_one(self, job: Job, allow_chunking: bool) -> bool:
        """Runs a single job and records its final status. Returns True on success."""
        with self._lock:
            job.status = STATUS_ENCODING
            job.start_time = time.time()
            job.status_text = "Probing..."
        self._emit("job_status", job)
        self._emit("queue_updated", self.jobs)
        suffix = f" ({job.worker_threads} parallel chunks)" if allow_chunking and job.worker_threads > 1 else ""
        self._emit("log", f"\n>>> Starting job: {job.title}{suffix}\n")

        try:
            success = self._execute_job(job, allow_chunking=allow_chunking)
        except Exception as e:
            # Keep the queue thread alive; a crash in one job should only fail that job.
            job.error_message = f"{type(e).__name__}: {e}"
            self._emit("log", f"\n[ERROR] Unexpected error in '{job.title}': {job.error_message}\n")
            success = False

        completed = False
        with self._lock:
            job.end_time = time.time()
            if self.cancel_requested:
                job.status = STATUS_CANCELED
                job.status_text = "Canceled"
            elif success:
                if job.status != STATUS_SKIPPED:
                    job.status = STATUS_COMPLETED
                    completed = True
            else:
                job.status = STATUS_FAILED
                job.status_text = "Failed"

        self._emit("job_status", job)
        self._emit("queue_updated", self.jobs)
        return completed

    def _queue_worker_loop(self):
        batch_start = time.time()
        completed_count = 0
        count_lock = threading.Lock()

        while self.is_running and not self.cancel_requested:
            with self._lock:
                pending_jobs = [j for j in self.jobs if j.status == STATUS_PENDING]
            if not pending_jobs:
                break

            if len(pending_jobs) == 1 and pending_jobs[0].worker_threads > 1:
                if self._run_one(pending_jobs[0], allow_chunking=True):
                    completed_count += 1
                continue

            max_workers_setting = max(
                self.max_workers or 1,
                max((j.worker_threads for j in pending_jobs), default=1)
            )
            actual_workers = min(max_workers_setting, len(pending_jobs))
            self._emit("log", f"\n>>> Processing {len(pending_jobs)} pending jobs with {actual_workers} parallel workers\n")

            job_work_queue: "queue.Queue[Job]" = queue.Queue()
            for j in pending_jobs:
                job_work_queue.put(j)

            def worker_thread_fn():
                nonlocal completed_count
                while self.is_running and not self.cancel_requested:
                    self._wait_while_paused()
                    try:
                        job_item = job_work_queue.get_nowait()
                    except queue.Empty:
                        break
                    # The job may have been removed or canceled while it waited.
                    if job_item.status != STATUS_PENDING or job_item not in self.jobs:
                        continue
                    if self._run_one(job_item, allow_chunking=False):
                        with count_lock:
                            completed_count += 1

            threads = [threading.Thread(target=worker_thread_fn, daemon=True) for _ in range(actual_workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        with self._lock:
            self.is_running = False
            self.is_paused = False
            self.cancel_requested = False

        self._emit("queue_status", {"is_running": False, "is_paused": False})

        if completed_count > 0:
            elapsed_sec = time.time() - batch_start
            el_m = int(elapsed_sec // 60)
            el_s = int(elapsed_sec % 60)
            time_str = f"{el_m}m {el_s:02d}s" if el_m > 0 else f"{el_s}s"
            self._emit("log", f"\n[Queue] Finished: completed {completed_count} job(s) in {time_str}.\n")
            self._emit("batch_completed", {
                "completed_count": completed_count,
                "time_str": time_str,
                "jobs": self.jobs
            })

    def _execute_job(self, job: Job, allow_chunking: bool = True) -> bool:
        """Skips already-encoded outputs, then runs a chunked or single-stream encode."""
        os.makedirs(job.output_dir, exist_ok=True)
        final_mkv = job.final_output_path

        if job.skip_existing and os.path.exists(final_mkv):
            size_mb = os.path.getsize(final_mkv) / (1024 * 1024)
            if size_mb > MIN_VALID_OUTPUT_MB:
                src_mb = os.path.getsize(job.source_path) / (1024 * 1024) if os.path.exists(job.source_path) else size_mb
                pct_diff = ((size_mb - src_mb) / src_mb * 100) if src_mb > 0 else 0
                sign = "+" if pct_diff > 0 else ""
                job.progress = 1.0
                job.final_size_mb = size_mb
                job.status = STATUS_SKIPPED
                job.status_text = f"Skipped ({size_mb:.1f} MB, {sign}{pct_diff:.0f}%)"
                self._emit("log", f"Skipping already encoded file: {os.path.basename(final_mkv)}\n")
                return True

        details = probe_file_details(job.source_path)
        if not details:
            job.error_message = "ffprobe could not read the source file."
            self._emit("log", f"[ERROR] Could not probe '{job.title}'. Is ffprobe installed?\n")
            return False

        duration = details.get('duration', 0.0)
        if allow_chunking and job.worker_threads > 1 and duration >= MIN_CHUNKING_DURATION_SEC:
            return self._execute_chunked_job(job, details, num_workers=job.worker_threads)
        return self._execute_standard_job(job, details)

    def _audio_filter(self, job: Job, stream: dict) -> Optional[str]:
        """Filter chain for one track, running the loudness measurement pass first for 2-pass mode."""
        measured = None
        target = norm_target_lufs(job.norm_mode)
        if target is not None and "2-Pass" in job.norm_mode:
            job.status_text = f"Measuring loudness of track {stream['index']}..."
            self._emit("job_status", job)
            downmix = not keeps_surround(stream, job.audio_channels)
            measured = measure_audio_loudness(job.source_path, stream['index'], target, downmix=downmix)
        return build_audio_filter(stream, job.audio_channels, job.norm_mode, measured)

    def _target_bitrate_k(self, job: Job, duration: float, audio_track_count: int) -> Optional[int]:
        if job.rate_control_mode != RATE_CONTROL_SIZE:
            return None
        return calculate_target_bitrate_k(job.target_size_mb, duration, int(job.audio_bitrate), audio_track_count)

    def _run_tracked_ffmpeg(self, proc_key: str, cmd: List[str], on_progress: Callable[[str, float], None]) -> Tuple[int, List[str]]:
        """Runs an FFmpeg command, reporting each progress line. Returns (returncode, last 50 lines)."""
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=flags
        )
        with self._lock:
            self.active_processes[proc_key] = proc

        output_tail: List[str] = []
        for line in proc.stdout:
            self._wait_while_paused()
            if self.cancel_requested:
                proc.terminate()
                break
            output_tail.append(line)
            if len(output_tail) > 50:
                output_tail.pop(0)
            m_time = TIME_PATTERN.search(line)
            if m_time:
                on_progress(line, parse_ffmpeg_time(m_time.group(1)))

        proc.wait()
        with self._lock:
            self.active_processes.pop(proc_key, None)
        return proc.returncode, output_tail

    @staticmethod
    def _parse_speed(line: str) -> Tuple[float, float]:
        spd_m = SPEED_PATTERN.search(line)
        fps_m = FPS_PATTERN.search(line)
        spd_val = float(spd_m.group(1).replace('x', '')) if spd_m else 1.0
        fps_val = float(fps_m.group(1)) if fps_m else 0.0
        if fps_val <= 0.1 and spd_val > 0.0:
            fps_val = spd_val * FALLBACK_FRAME_RATE
        return spd_val, fps_val

    def _encode_audio_and_mux(self, job: Job, details: dict, temp_dir: str, video_path: str) -> bool:
        """Encodes audio, muxes it with the encoded video and subtitles, and moves the result into place."""
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        sub_streams = details.get('subtitles', [])
        audio_streams, default_pos = select_audio_streams(job.audio_tracks, job.audio_language, details.get('audio', []))

        temp_audio = None
        if audio_streams:
            filters = [self._audio_filter(job, s) for s in audio_streams]
            job.status_text = "Encoding audio..."
            self._emit("job_status", job)
            temp_audio = os.path.join(temp_dir, f"audio_{job.stem}.mkv")
            audio_cmd = build_ffmpeg_audio_encode_cmd(job.source_path, temp_audio, job.audio_bitrate, audio_streams, filters)
            res_a = subprocess.run(audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
            if res_a.returncode != 0 or not os.path.exists(temp_audio):
                job.error_message = res_a.stderr or "Audio encode failed."
                self._emit("log", f"\n[ERROR] Audio encoding failed for '{job.title}':\n{job.error_message}\n")
                return False

        job.status_text = "Muxing final MKV..."
        self._emit("job_status", job)
        temp_final = os.path.join(temp_dir, f"final_{job.stem}.mkv")
        mux_cmd = build_mkvmerge_final_mux_cmd(
            output_path=temp_final,
            video_path=video_path,
            audio_path=temp_audio,
            source_path=job.source_path,
            audio_streams=audio_streams,
            default_audio_pos=default_pos,
            selected_subs=job.selected_subs or [s['index'] for s in sub_streams],
            sub_streams=sub_streams,
            audio_language=job.audio_language,
            smart_sub=job.smart_sub_matching,
            custom_sub_titles=job.custom_sub_titles
        )
        # mkvmerge exits with 1 for warnings, which still produce a valid file.
        res = subprocess.run(mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        if res.returncode > 1 or not os.path.exists(temp_final):
            job.error_message = (res.stdout or "") + "\n" + (res.stderr or "")
            self._emit("log", f"\n[ERROR] mkvmerge failed for '{job.title}' (code {res.returncode}):\n{job.error_message}\n")
            return False

        os.replace(temp_final, job.final_output_path)
        shutil.rmtree(temp_dir, ignore_errors=True)

        final_mb = os.path.getsize(job.final_output_path) / (1024 * 1024)
        job.final_size_mb = final_mb
        job.progress = 1.0
        pct_diff = ((final_mb - job.orig_size_mb) / job.orig_size_mb * 100) if job.orig_size_mb > 0 else 0
        sign = "+" if pct_diff > 0 else ""
        job.status_text = f"Done ({final_mb:.1f} MB, {sign}{pct_diff:.0f}%)"
        self._emit("log", f"[{job.stem}] Finished -> {final_mb:.1f} MB ({sign}{pct_diff:.1f}% vs source)\n")
        return True

    @staticmethod
    def _audio_track_count(job: Job, details: dict) -> int:
        return len(select_audio_streams(job.audio_tracks, job.audio_language, details.get('audio', []))[0])

    def _execute_standard_job(self, job: Job, details: dict) -> bool:
        """Single-process encode: video, then audio, then mkvmerge mux."""
        duration = details.get('duration', 0.0)
        temp_dir = os.path.join(job.output_dir, f".temp_enc_{job.id}")
        os.makedirs(temp_dir, exist_ok=True)
        temp_video = os.path.join(temp_dir, f"video_{job.stem}.mkv")

        target_k = self._target_bitrate_k(job, duration, self._audio_track_count(job, details))
        ffmpeg_cmd = build_ffmpeg_chunk_encode_cmd(
            source_path=job.source_path,
            chunk_output_path=temp_video,
            start_sec=0.0,
            duration_sec=0.0,
            cq=job.video_cq,
            preset=job.svt_preset,
            color_mode=job.bit_depth,
            film_grain=job.film_grain,
            resolution=job.resolution,
            is_source_hdr=details.get('is_hdr', False),
            rate_control_mode=job.rate_control_mode,
            target_bitrate_k=target_k
        )

        last_update = 0.0

        def on_progress(line: str, sec: float):
            nonlocal last_update
            now = time.time()
            if duration <= 0 or now - last_update <= 0.4:
                return
            last_update = now
            pct = min(sec / duration, 1.0)
            spd_val, fps_val = self._parse_speed(line)
            rem_sec = max(0, (duration - sec) / max(0.1, spd_val))
            eta = f"{int(rem_sec // 60)}m{int(rem_sec % 60):02d}s"
            job.progress = pct
            job.fps = fps_val
            job.speed = spd_val
            job.eta = eta
            job.status_text = f"{pct*100:4.1f}% | {fps_val:5.1f} fps ({spd_val:3.1f}x) | ETA: {eta}"
            self._emit("job_status", job)

        returncode, output_tail = self._run_tracked_ffmpeg(job.id, ffmpeg_cmd, on_progress)

        if self.cancel_requested or returncode != 0:
            if not self.cancel_requested:
                job.error_message = "".join(output_tail)
                self._emit("log", f"\n[ERROR] FFmpeg encoding failed for '{job.title}' (code {returncode}):\n{job.error_message}\n")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False

        return self._encode_audio_and_mux(job, details, temp_dir, temp_video)

    def _execute_chunked_job(self, job: Job, details: dict, num_workers: int) -> bool:
        """Splits the video into num_workers time ranges, encodes them in parallel, then joins them."""
        duration = details.get('duration', 0.0)
        temp_dir = os.path.join(job.output_dir, f".temp_chunked_{job.id}")
        os.makedirs(temp_dir, exist_ok=True)

        self._emit("log", f"[{job.stem}] Splitting {format_time_hms(duration)} video into {num_workers} parallel chunks...\n")

        chunk_duration = duration / num_workers
        chunk_tasks = []
        for i in range(num_workers):
            c_start = i * chunk_duration
            c_dur = duration - c_start if i == (num_workers - 1) else chunk_duration
            chunk_tasks.append({
                'index': i,
                'start_sec': c_start,
                'duration_sec': c_dur,
                'out_path': os.path.join(temp_dir, f"chunk_{i:03d}.mkv"),
                'progress': 0.0,
                'fps': 0.0,
                'speed': 0.0
            })

        job.chunks = {t['index']: t for t in chunk_tasks}
        self._emit("job_status", job)

        # Every chunk shares the same average bitrate, so the per-chunk target equals the whole-file target.
        target_k = self._target_bitrate_k(job, duration, self._audio_track_count(job, details))
        chunk_lock = threading.Lock()
        chunk_failures = []

        def chunk_worker(task: dict):
            cmd = build_ffmpeg_chunk_encode_cmd(
                source_path=job.source_path,
                chunk_output_path=task['out_path'],
                start_sec=task['start_sec'],
                duration_sec=task['duration_sec'],
                cq=job.video_cq,
                preset=job.svt_preset,
                color_mode=job.bit_depth,
                film_grain=job.film_grain,
                resolution=job.resolution,
                is_source_hdr=details.get('is_hdr', False),
                rate_control_mode=job.rate_control_mode,
                target_bitrate_k=target_k
            )
            c_dur = task['duration_sec']
            last_up = 0.0

            def on_progress(line: str, sec: float):
                nonlocal last_up
                now = time.time()
                if c_dur <= 0 or now - last_up <= 0.4:
                    return
                last_up = now
                spd_val, fps_val = self._parse_speed(line)
                with chunk_lock:
                    task['progress'] = min(sec / c_dur, 1.0)
                    task['fps'] = fps_val
                    task['speed'] = spd_val

                    total_pct = sum(t['progress'] for t in chunk_tasks) / num_workers
                    total_fps = sum(t['fps'] for t in chunk_tasks)
                    total_spd = sum(t['speed'] for t in chunk_tasks)
                    rem_sec = max(0, (duration * (1.0 - total_pct)) / max(0.1, total_spd))
                    eta = f"{int(rem_sec // 60)}m{int(rem_sec % 60):02d}s"

                    job.progress = total_pct
                    job.fps = total_fps
                    job.speed = total_spd
                    job.eta = eta
                    job.status_text = f"{total_pct*100:4.1f}% | {total_fps:5.1f} fps ({total_spd:3.1f}x) | ETA: {eta} ({num_workers} chunks)"
                    self._emit("job_status", job)

            returncode, output_tail = self._run_tracked_ffmpeg(f"{job.id}_chunk_{task['index']}", cmd, on_progress)
            if returncode != 0 and not self.cancel_requested:
                with chunk_lock:
                    chunk_failures.append(task['index'])
                    job.error_message = "".join(output_tail)

        threads = [threading.Thread(target=chunk_worker, args=(task,), daemon=True) for task in chunk_tasks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if self.cancel_requested or chunk_failures:
            if chunk_failures:
                self._emit("log", f"\n[ERROR] Chunk(s) {sorted(chunk_failures)} failed for '{job.title}':\n{job.error_message}\n")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False

        job.status_text = "Joining chunks..."
        self._emit("job_status", job)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        temp_stitched_v = os.path.join(temp_dir, f"stitched_{job.stem}.mkv")
        concat_cmd = build_mkvmerge_chunk_concat_cmd(temp_stitched_v, [t['out_path'] for t in chunk_tasks])
        res_c = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        if res_c.returncode > 1 or not os.path.exists(temp_stitched_v):
            job.error_message = (res_c.stdout or "") + "\n" + (res_c.stderr or "")
            self._emit("log", f"\n[ERROR] Joining chunks failed for '{job.title}':\n{job.error_message}\n")
            return False

        return self._encode_audio_and_mux(job, details, temp_dir, temp_stitched_v)
