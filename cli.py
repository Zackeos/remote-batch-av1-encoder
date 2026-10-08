"""
Headless batch encoder: scans a folder, queues every video file and runs the queue
with a progress line in the terminal.
"""

import os
import sys
import time
import argparse

from core.constants import (
    APP_TITLE, COLOR_FORMAT_AUTO, COLOR_FORMAT_OPTIONS, NORM_MODE_OPTIONS,
    AUDIO_TRACK_OPTIONS, RATE_CONTROL_CQ, RESOLUTION_SOURCE, normalize_language
)
from core.job import Job, STATUS_ENCODING, STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_SKIPPED
from core.queue_manager import QueueManager
from core.parser import scan_video_files
from core.probe import probe_file_details
from core.settings import load_settings


def main():
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        description=f"{APP_TITLE} headless batch encoder (SVT-AV1 + Opus)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--input", "-i", required=True, help="Folder containing source video files")
    parser.add_argument("--output", "-o", default="downloads/encoded", help="Folder for encoded MKV files")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Number of files encoded in parallel")
    parser.add_argument("--cq", default="22", help="Constant quality (CRF) value")
    parser.add_argument("--preset", default="6", help="SVT-AV1 preset (lower is slower and better)")
    parser.add_argument("--grain", default="0", help="Film grain synthesis strength (0 disables)")
    parser.add_argument("--bit-depth", default=COLOR_FORMAT_AUTO, choices=COLOR_FORMAT_OPTIONS, help="Output color format")
    parser.add_argument("--audio-bitrate", default="96", help="Opus bitrate per track in kbps")
    parser.add_argument("--audio-tracks", default=AUDIO_TRACK_OPTIONS[0], choices=AUDIO_TRACK_OPTIONS, help="Which audio tracks to keep")
    parser.add_argument("--audio-language", default=load_settings()["audio_language"], help="Preferred audio language (ISO 639 code, e.g. eng, spa, fra)")
    parser.add_argument("--norm", default=NORM_MODE_OPTIONS[0], choices=NORM_MODE_OPTIONS, help="Loudness normalization")
    parser.add_argument("--recursive", "-r", action="store_true", help="Also scan subfolders")
    args = parser.parse_args()

    if not os.path.isdir(args.input):
        print(f"[ERROR] Input folder '{args.input}' does not exist.")
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)

    print(f"{APP_TITLE} - headless batch encode")
    print(f"  Input:    {args.input}")
    print(f"  Output:   {args.output}")
    print(f"  Settings: CQ {args.cq} | preset {args.preset} | grain {args.grain} | {args.audio_bitrate}k Opus")
    print(f"  Workers:  {args.workers}\n")

    files = scan_video_files(args.input, recursive=args.recursive)
    if not files:
        print("No supported video files found.")
        sys.exit(0)

    # Subtitle track indexes are taken from the first file and applied to the whole batch.
    sub_streams = probe_file_details(files[0][3]).get('subtitles', [])
    selected_subs = [s['index'] for s in sub_streams]

    qm = QueueManager()
    qm.max_workers = args.workers

    for rel_dir, _season, _episode, f_path, _label in files:
        target_out = os.path.join(args.output, rel_dir) if rel_dir != "." else args.output
        os.makedirs(target_out, exist_ok=True)
        qm.add_job(Job(
            source_path=f_path,
            output_dir=target_out,
            video_cq=args.cq,
            svt_preset=args.preset,
            bit_depth=args.bit_depth,
            film_grain=args.grain,
            audio_bitrate=args.audio_bitrate,
            norm_mode=args.norm,
            audio_tracks=args.audio_tracks,
            audio_language=normalize_language(args.audio_language),
            selected_subs=selected_subs,
            resolution=RESOLUTION_SOURCE,
            rate_control_mode=RATE_CONTROL_CQ,
            worker_threads=args.workers
        ))

    print(f"Queued {len(qm.jobs)} file(s). Starting...\n")
    start_time = time.time()
    qm.start_queue()

    try:
        while qm.is_running:
            time.sleep(1.0)
            jobs = list(qm.jobs)
            active = [j for j in jobs if j.status == STATUS_ENCODING]
            done = sum(1 for j in jobs if j.status in (STATUS_COMPLETED, STATUS_SKIPPED))
            pending = sum(1 for j in jobs if j.status == STATUS_PENDING)
            line = f"\rProgress: {done}/{len(jobs)} done | {len(active)} active | {pending} queued"
            speeds = [f"{j.speed:.1f}x" for j in active if j.speed > 0]
            if speeds:
                line += f" | speed: {', '.join(speeds[:3])}"
            sys.stdout.write(line.ljust(100))
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nInterrupted. Canceling queue...")
        qm.cancel_all()
        sys.exit(1)

    elapsed = int(time.time() - start_time)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    print(f"\n\nFinished in {h:02d}h {m:02d}m {s:02d}s")
    for j in qm.jobs:
        print(f"  [{j.status.upper():9}] {j.title}: {j.status_text}")

    sys.exit(1 if any(j.status == STATUS_FAILED for j in qm.jobs) else 0)


if __name__ == "__main__":
    main()
