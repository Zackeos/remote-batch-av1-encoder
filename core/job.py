"""
Data model and state for a single encoding job.
"""

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from .constants import (
    RATE_CONTROL_CQ, RESOLUTION_SOURCE, COLOR_FORMAT_AUTO, NORM_MODE_OPTIONS,
    AUDIO_TRACK_OPTIONS, DEFAULT_AUDIO_LANGUAGE
)

STATUS_PENDING = "Pending"
STATUS_ENCODING = "Encoding"
STATUS_PAUSED = "Paused"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"
STATUS_CANCELED = "Canceled"
STATUS_SKIPPED = "Skipped"

@dataclass
class Job:
    source_path: str
    output_dir: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    stem: str = ""

    # Configuration snapshot (frozen per job)
    rate_control_mode: str = RATE_CONTROL_CQ
    video_cq: str = "22"
    target_size_val: str = "5.0"
    target_size_unit: str = "GB"
    target_size_mb: Optional[float] = None
    resolution: str = RESOLUTION_SOURCE
    svt_preset: str = "5"
    bit_depth: str = COLOR_FORMAT_AUTO
    film_grain: str = "Disabled"
    audio_bitrate: str = "96"
    audio_channels: str = "Stereo (2.0)"
    norm_mode: str = NORM_MODE_OPTIONS[0]
    audio_tracks: str = AUDIO_TRACK_OPTIONS[0]
    audio_language: str = DEFAULT_AUDIO_LANGUAGE
    selected_subs: List[int] = field(default_factory=list)
    custom_sub_titles: Dict[int, str] = field(default_factory=dict)
    smart_sub_matching: bool = True
    worker_threads: int = 5
    skip_existing: bool = True

    # Runtime execution state
    status: str = STATUS_PENDING
    progress: float = 0.0
    fps: float = 0.0
    speed: float = 0.0
    eta: str = ""
    status_text: str = "Queued"
    error_message: str = ""
    chunks: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    log_tail: List[str] = field(default_factory=list)

    start_time: float = 0.0
    end_time: float = 0.0
    orig_size_mb: float = 0.0
    final_size_mb: float = 0.0

    def __post_init__(self):
        if not self.stem:
            base = os.path.basename(self.source_path)
            self.stem = os.path.splitext(base)[0]
        if not self.title:
            self.title = os.path.basename(self.source_path)
        if os.path.exists(self.source_path):
            try:
                self.orig_size_mb = os.path.getsize(self.source_path) / (1024 * 1024)
            except OSError:
                pass

    @property
    def final_output_path(self) -> str:
        return os.path.join(self.output_dir, f"{self.stem}.mkv")

    @property
    def is_finished(self) -> bool:
        return self.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED, STATUS_SKIPPED)

    @property
    def elapsed_str(self) -> str:
        if self.start_time <= 0:
            return ""
        end = self.end_time if self.end_time > 0 else time.time()
        sec = int(end - self.start_time)
        m = sec // 60
        s = sec % 60
        return f"{m}m {s:02d}s" if m > 0 else f"{s}s"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "stem": self.stem,
            "source_path": self.source_path,
            "output_dir": self.output_dir,
            "rate_control_mode": self.rate_control_mode,
            "video_cq": self.video_cq,
            "target_size_mb": self.target_size_mb,
            "svt_preset": self.svt_preset,
            "bit_depth": self.bit_depth,
            "film_grain": self.film_grain,
            "resolution": self.resolution,
            "audio_bitrate": self.audio_bitrate,
            "audio_channels": self.audio_channels,
            "norm_mode": self.norm_mode,
            "audio_tracks": self.audio_tracks,
            "audio_language": self.audio_language,
            "status": self.status,
            "progress": self.progress,
            "fps": self.fps,
            "speed": self.speed,
            "eta": self.eta,
            "status_text": self.status_text,
            "error_message": self.error_message,
            "orig_size_mb": round(self.orig_size_mb, 1),
            "final_size_mb": round(self.final_size_mb, 1),
            "elapsed": self.elapsed_str,
            "final_output_path": self.final_output_path
        }
