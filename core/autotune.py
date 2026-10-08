"""
Auto-tune benchmark: extracts four 7-second slices from across a file (28 s total),
encodes them with several CQ / film-grain profiles, scores each with VMAF, and
recommends the smallest profile that stays at or above the VMAF target.
"""

import os
import re
import subprocess
import time
from typing import List, Dict, Any, Optional, Callable, Tuple
from .constants import RESOLUTION_SOURCE
from .encoder import (
    build_svtav1_params, get_ffmpeg_exe, get_scale_filter, parse_ffmpeg_time,
    TIME_PATTERN, SPEED_PATTERN, FPS_PATTERN
)

TOTAL_SAMPLE_DURATION_SEC = 28.0
VMAF_TARGET = 95.0
# Grain must shrink output by at least this much versus the same CQ without grain to count as worthwhile.
GRAIN_SAVING_THRESHOLD = 0.93
# Assumed runtime (about one TV episode) when the source duration is unknown.
FALLBACK_DURATION_SEC = 1420.0

AUTOTUNE_PROFILES = [
    {"name": "1. Reference", "cq": "20", "grain": "Disabled", "desc": "Highest-quality reference point"},
    {"name": "2. High quality", "cq": "22", "grain": "Disabled", "desc": "No grain synthesis"},
    {"name": "3. Balanced", "cq": "24", "grain": "Disabled", "desc": "No grain synthesis"},
    {"name": "4. Efficient", "cq": "26", "grain": "Disabled", "desc": "No grain synthesis"},
    {"name": "5. Smallest", "cq": "28", "grain": "Disabled", "desc": "Highest compression tested"},
    {"name": "6. High quality + light grain", "cq": "22", "grain": "Light (8)", "desc": "Compare against profile 2"},
    {"name": "7. Balanced + medium grain", "cq": "24", "grain": "Medium (16)", "desc": "Compare against profile 3"},
]

def calculate_vmaf_score(
    reference_path: str,
    distorted_path: str,
    start_sec: Optional[str] = None,
    duration_sec: Optional[int] = None,
    resolution: str = RESOLUTION_SOURCE,
    threads: int = 8,
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Optional[float]:
    """Computes the mean VMAF score of distorted_path against reference_path (None if libvmaf fails).
    Timestamps are reset on both inputs so frames are compared 1:1."""
    scale_filter = ""
    if "1080p" in resolution:
        scale_filter = "scale=1920:-2:flags=bicubic,"
    elif "720p" in resolution:
        scale_filter = "scale=1280:-2:flags=bicubic,"
    elif "1440p" in resolution:
        scale_filter = "scale=2560:-2:flags=bicubic,"
    elif "4K" in resolution:
        scale_filter = "scale=3840:-2:flags=bicubic,"

    filter_str = f"[0:v]setpts=N/FRAME_RATE/TB[v0];[1:v]{scale_filter}setpts=N/FRAME_RATE/TB[v1];[v0][v1]libvmaf=n_threads={threads}:n_subsample=1"

    # libvmaf expects the distorted clip first and the reference second.
    cmd = [get_ffmpeg_exe(), '-y', '-i', distorted_path]
    if start_sec:
        cmd.extend(['-ss', str(start_sec)])
    if duration_sec:
        cmd.extend(['-t', str(duration_sec)])
    cmd.extend(['-i', reference_path])

    cmd.extend([
        '-filter_complex', filter_str,
        '-f', 'null', '-'
    ])

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=flags
    )

    score = None
    last_up = 0
    total_dur = float(duration_sec) if duration_sec else TOTAL_SAMPLE_DURATION_SEC

    for line in proc.stdout:
        m_vmaf = re.search(r'VMAF score:\s*([\d.]+)', line)
        if m_vmaf:
            score = float(m_vmaf.group(1))

        m_time = TIME_PATTERN.search(line)
        if m_time and total_dur > 0 and progress_callback:
            pct = min(parse_ffmpeg_time(m_time.group(1)) / total_dur, 1.0)
            now = time.time()
            if now - last_up > 0.3:
                last_up = now
                spd_m = SPEED_PATTERN.search(line)
                fps_m = FPS_PATTERN.search(line)
                spd = spd_m.group(1) if spd_m else "1.0x"
                fps = fps_m.group(1) if fps_m else "0.0"
                progress_callback(pct, f"Calculating VMAF: {pct*100:4.1f}% | {fps} fps ({spd})")

    proc.wait()
    return score

def _ffmpeg_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def extract_multi_slice_reference(
    ep_path: str,
    output_path: str,
    duration: float,
    flags: int,
    resolution: str = RESOLUTION_SOURCE
) -> bool:
    """
    Extracts four 7-second slices at 12%, 35%, 60% and 85% of the runtime and joins
    them into a lossless 28-second reference clip, applying any requested downscale.
    """
    dur = max(60.0, duration)
    slices = [
        (_ffmpeg_timestamp(dur * 0.12), 7),
        (_ffmpeg_timestamp(dur * 0.35), 7),
        (_ffmpeg_timestamp(dur * 0.60), 7),
        (_ffmpeg_timestamp(dur * 0.85), 7)
    ]

    scale_f = get_scale_filter(resolution)
    filter_complex = '[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]'
    if scale_f:
        filter_complex += f';[v]{scale_f}[v_scaled]'
        map_target = '[v_scaled]'
    else:
        map_target = '[v]'

    cmd = [get_ffmpeg_exe(), '-y', '-hwaccel', 'auto']
    for start_t, slice_len in slices:
        cmd.extend(['-ss', start_t, '-t', str(slice_len), '-i', ep_path])

    cmd.extend([
        '-filter_complex', filter_complex,
        '-map', map_target,
        '-c:v', 'libx264',
        '-crf', '0',
        '-preset', 'ultrafast',
        '-pix_fmt', 'yuv420p10le',
        output_path
    ])
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    return res.returncode == 0 and os.path.exists(output_path)

def run_autotune_benchmark(
    ep_path: str,
    output_dir: str,
    is_10bit: bool,
    total_files: int,
    file_duration: float = FALLBACK_DURATION_SEC,
    resolution: str = RESOLUTION_SOURCE,
    status_callback: Optional[Callable[[str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """
    Benchmarks every profile on the multi-slice sample, one at a time.
    Returns (results, recommended profile or None, temp directory holding the clips).
    """
    temp_dir = os.path.join(output_dir, ".autotune_tests")
    os.makedirs(temp_dir, exist_ok=True)

    pix_fmt = "yuv420p10le" if is_10bit else "yuv420p"
    total_sec = max(1, total_files) * (file_duration if file_duration > 0 else FALLBACK_DURATION_SEC)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    ref_clip = os.path.join(temp_dir, "ref_multislice_28s.mkv")
    if status_callback:
        status_callback("[Auto-Tune] Extracting sample slices...")
    if log_callback:
        res_info = f" ({resolution})" if resolution != RESOLUTION_SOURCE else ""
        log_callback(f"[Auto-Tune] Extracting four 7s slices (12%, 35%, 60%, 85% of runtime) into a lossless reference{res_info}...\n")

    extract_multi_slice_reference(ep_path, ref_clip, file_duration, flags, resolution)
    source_to_encode = ref_clip if os.path.exists(ref_clip) else ep_path

    profiles_to_test = AUTOTUNE_PROFILES
    results = []
    num_tests = len(profiles_to_test)

    # Profiles run one at a time so each encode gets the whole CPU.
    for idx, p in enumerate(profiles_to_test):
        if cancel_check and cancel_check():
            break

        msg = f"[Auto-Tune] Testing {p['name']} ({idx+1}/{num_tests})..."
        if status_callback:
            status_callback(msg)
        if log_callback:
            log_callback(f"[Auto-Tune] Testing {p['name']} (CQ={p['cq']}, Grain={p['grain']})...\n")

        clip_out = os.path.join(temp_dir, f"test_{idx+1}_cq{p['cq']}.mkv")
        svt_params = build_svtav1_params(p['grain'])

        cmd = [
            get_ffmpeg_exe(), '-y',
            '-hwaccel', 'auto',
            '-i', source_to_encode,
            '-map', '0:v:0',
            '-c:v', 'libsvtav1',
            '-crf', p['cq'],
            '-preset', '5',
            '-pix_fmt', pix_fmt,
            '-svtav1-params', svt_params,
            '-color_primaries', 'bt709',
            '-color_trc', 'bt709',
            '-colorspace', 'bt709',
            '-color_range', 'tv',
            '-an', clip_out
        ]

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=flags
        )

        last_t = 0
        for line in proc.stdout:
            if cancel_check and cancel_check():
                proc.terminate()
                break
            m = TIME_PATTERN.search(line)
            if m:
                pct = min(100.0, (parse_ffmpeg_time(m.group(1)) / TOTAL_SAMPLE_DURATION_SEC) * 100)
                if progress_callback:
                    step_prog = (idx + (pct / 100.0)) / num_tests
                    progress_callback(min(1.0, max(0.0, step_prog)))
                now = time.time()
                if now - last_t > 0.25:
                    last_t = now
                    spd_m = SPEED_PATTERN.search(line)
                    fps_m = FPS_PATTERN.search(line)
                    fps_val = float(fps_m.group(1)) if fps_m else 0.0
                    if fps_val <= 0.1 and spd_m:
                        fps_val = float(spd_m.group(1).rstrip('x')) * 23.976
                    if status_callback:
                        status_callback(f"[Auto-Tune {idx+1}/{num_tests}] {p['name']}: {pct:.1f}% ({fps_val:.1f} fps)")

        proc.wait()

        if cancel_check and cancel_check():
            break

        size_mb = os.path.getsize(clip_out) / (1024 * 1024) if os.path.exists(clip_out) else 0.0
        proj_total_gb = (size_mb / TOTAL_SAMPLE_DURATION_SEC) * total_sec / 1024.0

        if status_callback:
            status_callback(f"[Auto-Tune {idx+1}/{num_tests}] Computing VMAF visual score for {p['name']}...")

        vmaf_val = calculate_vmaf_score(
            reference_path=source_to_encode,
            distorted_path=clip_out
        )
        if vmaf_val is None or size_mb <= 0:
            if log_callback:
                log_callback(f"[Auto-Tune] {p['name']}: encode or VMAF measurement failed, profile skipped.\n")
            continue
        score = vmaf_val

        results.append({
            'name': p['name'],
            'desc': p['desc'],
            'cq': p['cq'],
            'grain': p['grain'],
            'clip_size_mb': size_mb,
            'proj_gb': proj_total_gb,
            'vmaf': score,
            'clip_path': clip_out
        })
        if log_callback:
            log_callback(f"[Auto-Tune] Result: Size={size_mb:.2f}MB (Total ~{proj_total_gb:.1f}GB), VMAF={score:.2f}\n")

    if not results:
        return [], None, temp_dir

    return results, recommend_profile(results, total_files), temp_dir


def _format_size_gb(gb: float) -> str:
    return f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1024:.0f} MB"


def recommend_profile(results: List[Dict[str, Any]], total_files: int = 1) -> Optional[Dict[str, Any]]:
    """
    Picks the smallest profile scoring at least VMAF_TARGET, or the highest-scoring
    profile if none reach it. Adds a human-readable 'reason' to the chosen entry.
    """
    if not results:
        return None

    lossless_candidates = [r for r in results if r['vmaf'] >= VMAF_TARGET]
    baseline_ref = results[0]

    if lossless_candidates:
        clean_lossless = [r for r in lossless_candidates if r['grain'] == "Disabled"]
        grain_lossless = [r for r in lossless_candidates if r['grain'] != "Disabled"]

        grain_is_effective = False
        for g in grain_lossless:
            matching_clean = next((c for c in clean_lossless if c['cq'] == g['cq']), None)
            if matching_clean and g['proj_gb'] < (matching_clean['proj_gb'] * GRAIN_SAVING_THRESHOLD):
                grain_is_effective = True
                break

        best = min(lossless_candidates, key=lambda x: x['proj_gb'])
        base_gb = baseline_ref['proj_gb']
        saved_pct = (1.0 - (best['proj_gb'] / base_gb)) * 100 if base_gb > 0 else 0.0
        gb_saved = max(0.0, base_gb - best['proj_gb'])

        scope = "for the whole batch" if total_files > 1 else "for this file"
        grain_note = " Film grain synthesis reduced size at the same CQ." if best['grain'] != "Disabled" and grain_is_effective else ""
        best['reason'] = (
            f"Smallest profile with VMAF >= {VMAF_TARGET:.0f} on the sampled slices ({best['vmaf']:.1f}). "
            f"Projected {saved_pct:.0f}% smaller (~{_format_size_gb(gb_saved)}) {scope} than '{baseline_ref['name']}'.{grain_note}"
        )
    else:
        best = max(results, key=lambda x: x['vmaf'])
        best['reason'] = f"No profile reached VMAF {VMAF_TARGET:.0f}; this one scored highest ({best['vmaf']:.1f})."

    return best
