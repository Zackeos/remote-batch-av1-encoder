"""
FFmpeg / mkvmerge command construction, FFmpeg progress regexes, and target-size
bitrate calculation. Nothing in this module runs a process; it only builds commands.
"""

import os
import re
import shutil
from typing import List, Dict, Any, Optional, Tuple
from .constants import (
    RATE_CONTROL_CQ, RATE_CONTROL_SIZE,
    RESOLUTION_SOURCE, RESOLUTION_1080P, RESOLUTION_720P, RESOLUTION_1440P, RESOLUTION_4K,
    COLOR_FORMAT_AUTO, COLOR_FORMAT_10BIT_HDR, COLOR_FORMAT_8BIT_SDR,
    DEFAULT_TRUE_PEAK, DEFAULT_LRA, normalize_language, language_name
)

TIME_PATTERN = re.compile(r'time=(\d{2}:\d{2}:\d{2}\.\d{2})')
SPEED_PATTERN = re.compile(r'speed=\s*([\d.]+x)')
FPS_PATTERN = re.compile(r'fps=\s*([\d.]+)')

# Fraction of the target size reserved for Matroska container overhead.
CONTAINER_OVERHEAD_RATIO = 0.005
MIN_VIDEO_MB = 10.0
MIN_VIDEO_BITRATE_K = 200

def parse_ffmpeg_time(timestamp: str) -> float:
    """Converts an FFmpeg 'HH:MM:SS.xx' progress timestamp to seconds."""
    h, m, s = timestamp.split(':')
    return float(h) * 3600 + float(m) * 60 + float(s)

def calculate_target_bitrate_k(
    target_size_mb: Optional[float],
    duration_sec: float,
    audio_bitrate_k: int,
    audio_track_count: int
) -> Optional[int]:
    """
    Returns the video bitrate in kbps needed to land on target_size_mb once the
    audio tracks and container overhead are subtracted. Returns None when there is
    no usable target or duration.
    """
    if not target_size_mb or target_size_mb <= 0 or duration_sec <= 0:
        return None
    audio_mb = (audio_bitrate_k * max(0, audio_track_count) * duration_sec) / (8 * 1024)
    container_mb = target_size_mb * CONTAINER_OVERHEAD_RATIO
    video_mb = max(MIN_VIDEO_MB, target_size_mb - audio_mb - container_mb)
    return max(MIN_VIDEO_BITRATE_K, int((video_mb * 8 * 1024) / duration_sec))

def get_ffmpeg_exe() -> str:
    """Prefers a direct Chocolatey install over its shim, then falls back to PATH."""
    candidates = [
        r"C:\ProgramData\chocolatey\lib\ffmpeg-full\tools\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("ffmpeg") or "ffmpeg"

def get_ffprobe_exe() -> str:
    """Prefers a direct Chocolatey install over its shim, then falls back to PATH."""
    candidates = [
        r"C:\ProgramData\chocolatey\lib\ffmpeg-full\tools\ffmpeg\bin\ffprobe.exe",
        r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffprobe.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("ffprobe") or "ffprobe"

def get_mkvmerge_exe() -> str:
    """Checks the default Windows install locations, then falls back to PATH."""
    candidates = [
        r"C:\Program Files\MKVToolNix\mkvmerge.exe",
        r"C:\ProgramData\chocolatey\lib\mkvtoolnix\tools\mkvmerge.exe"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("mkvmerge") or "mkvmerge"

def get_scale_filter(resolution: str) -> Optional[str]:
    """Returns the FFmpeg scale filter string with high-quality lanczos scaling."""
    if resolution == RESOLUTION_1080P or "1080p" in resolution:
        return "scale='min(1920,iw)':-2:flags=lanczos"
    elif resolution == RESOLUTION_720P or "720p" in resolution:
        return "scale='min(1280,iw)':-2:flags=lanczos"
    elif resolution == RESOLUTION_1440P or "1440p" in resolution:
        return "scale='min(2560,iw)':-2:flags=lanczos"
    elif resolution == RESOLUTION_4K or "4K" in resolution:
        return "scale='min(3840,iw)':-2:flags=lanczos"
    return None

def build_svtav1_params(film_grain: str, target_bitrate_k: Optional[int] = None, lp: Optional[int] = None) -> str:
    """Builds the -svtav1-params string (VBR target, film grain, logical processor cap)."""
    params = ['tune=0']

    # Limit parallelism per SVT-AV1 process so many parallel encoders don't each
    # allocate buffers for every core. SVT-AV1 1.x treats lp as a thread count;
    # newer releases treat it as a parallelism level from 0 to 6.
    if lp and lp > 0:
        params.append(f"lp={lp}")
    else:
        params.append("lp=8")

    if target_bitrate_k and target_bitrate_k > 0:
        params.append(f"rc=1:tbr={target_bitrate_k}")
    if film_grain and film_grain != "Disabled" and film_grain != "0":
        m = re.search(r'(\d+)', film_grain)
        if m:
            val = int(m.group(1))
            if val > 0:
                params.append(f"film-grain={val}")
                denoise_val = 0 if val <= 8 else 1
                params.append(f"film-grain-denoise={denoise_val}")
    return ':'.join(params)

def build_video_filter_and_colorspace(
    resolution: str,
    color_mode: str,
    is_source_hdr: bool
):
    """
    Decides whether to tone-map HDR to SDR, keep HDR10, or pass SDR through.
    Returns (filter string or None, color signaling args, pix_fmt, needs_vulkan).
    """
    filters = []
    use_vulkan = False

    output_hdr = False
    if color_mode == COLOR_FORMAT_10BIT_HDR:
        output_hdr = True
    elif color_mode == COLOR_FORMAT_AUTO and is_source_hdr and resolution not in (RESOLUTION_1080P, RESOLUTION_720P):
        output_hdr = True

    # HDR source with SDR output: tone-map with libplacebo (requires a Vulkan device)
    if is_source_hdr and not output_hdr:
        filters.append("libplacebo=tonemapping=auto:colorspace=bt709:color_primaries=bt709:color_trc=bt709:format=yuv420p10le")
        use_vulkan = True
        color_signals = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv']
        pix_fmt = 'yuv420p' if color_mode == COLOR_FORMAT_8BIT_SDR else 'yuv420p10le'
    elif output_hdr:
        color_signals = ['-color_primaries', 'bt2020', '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc', '-color_range', 'tv']
        pix_fmt = 'yuv420p10le'
    else:
        color_signals = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv']
        pix_fmt = 'yuv420p' if color_mode == COLOR_FORMAT_8BIT_SDR else 'yuv420p10le'

    scale_f = get_scale_filter(resolution)
    if scale_f:
        filters.append(scale_f)

    vf_str = ",".join(filters) if filters else None
    return vf_str, color_signals, pix_fmt, use_vulkan

def build_ffmpeg_chunk_encode_cmd(
    source_path: str,
    chunk_output_path: str,
    start_sec: float,
    duration_sec: float,
    cq: str,
    preset: str,
    color_mode: str,
    film_grain: str,
    resolution: str = RESOLUTION_SOURCE,
    is_source_hdr: bool = False,
    rate_control_mode: str = RATE_CONTROL_CQ,
    target_bitrate_k: Optional[int] = None
) -> List[str]:
    """Builds the FFmpeg command for a video-only encode of [start_sec, start_sec + duration_sec).
    A start and duration of 0 encode the whole file."""
    vf_str, color_signals, pix_fmt, use_vulkan = build_video_filter_and_colorspace(resolution, color_mode, is_source_hdr)

    cmd = [get_ffmpeg_exe(), '-y']
    if use_vulkan:
        cmd.extend(['-init_hw_device', 'vulkan'])

    cmd.extend(['-hwaccel', 'auto'])
    if start_sec > 0:
        cmd.extend(['-ss', f"{start_sec:.3f}"])
    if duration_sec > 0:
        cmd.extend(['-t', f"{duration_sec:.3f}"])

    cmd.extend([
        '-i', source_path,
        '-map', '0:v:0',
        '-c:v', 'libsvtav1',
    ])

    if vf_str:
        cmd.extend(['-vf', vf_str])

    if rate_control_mode == RATE_CONTROL_SIZE and target_bitrate_k and target_bitrate_k > 0:
        # Plain VBR: SVT-AV1 rejects -maxrate outside CRF mode.
        cmd.extend(['-b:v', f'{target_bitrate_k}k'])
    else:
        cmd.extend(['-crf', cq])

    svt_params = build_svtav1_params(film_grain, target_bitrate_k=target_bitrate_k if rate_control_mode == RATE_CONTROL_SIZE else None)
    cmd.extend([
        '-preset', preset,
        '-pix_fmt', pix_fmt,
        '-svtav1-params', svt_params,
    ])
    cmd.extend(color_signals)
    cmd.extend(['-an', '-sn', '-dn', chunk_output_path])
    return cmd

def keeps_surround(stream: Dict[str, Any], audio_channel_layout: str) -> bool:
    """True when a 5.1+ source track should stay multichannel instead of being downmixed."""
    return "5.1" in audio_channel_layout and stream.get('channels', 2) >= 6


def norm_target_lufs(norm_mode: str) -> Optional[float]:
    """Integrated loudness target for a normalization option, or None when disabled."""
    if "Disabled" in norm_mode:
        return None
    return -23.0 if "-23" in norm_mode else -20.0


def build_audio_filter(
    stream: Dict[str, Any],
    audio_channel_layout: str,
    norm_mode: str,
    measured: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Builds the filter chain for one audio track: an optional stereo downmix followed by
    EBU R128 loudnorm. With pass-1 measurements the loudnorm runs in linear mode.
    """
    parts = [] if keeps_surround(stream, audio_channel_layout) else ["aformat=channel_layouts=stereo"]
    target = norm_target_lufs(norm_mode)
    if target is not None:
        loudnorm = f"loudnorm=I={target}:TP={DEFAULT_TRUE_PEAK}:LRA={DEFAULT_LRA}"
        if measured:
            loudnorm += (
                f":measured_I={measured.get('input_i', '-28.0')}"
                f":measured_TP={measured.get('input_tp', '-3.0')}"
                f":measured_LRA={measured.get('input_lra', '11.0')}"
                f":measured_thresh={measured.get('input_thresh', '-38.0')}:linear=true"
            )
        parts.append(loudnorm)
    return ",".join(parts) or None


def build_ffmpeg_audio_encode_cmd(
    source_path: str,
    temp_audio_path: str,
    audio_bitrate: str,
    audio_streams: List[Dict[str, Any]],
    audio_filters: List[Optional[str]]
) -> List[str]:
    """Builds the FFmpeg command that encodes each selected audio track to Opus in an MKV."""
    cmd = [get_ffmpeg_exe(), '-y', '-i', source_path, '-vn', '-sn', '-dn']
    for out_idx, (stream, af) in enumerate(zip(audio_streams, audio_filters)):
        cmd.extend(['-map', f"0:{stream['index']}"])
        if af:
            cmd.extend([f'-filter:a:{out_idx}', af])
        cmd.extend([
            f'-c:a:{out_idx}', 'libopus',
            f'-b:a:{out_idx}', f'{audio_bitrate}k',
            f'-ar:a:{out_idx}', '48000',
        ])
    cmd.extend(['-map_metadata', '-1', '-map_chapters', '-1', temp_audio_path])
    return cmd


def build_mkvmerge_chunk_concat_cmd(output_video_path: str, chunk_paths: List[str]) -> List[str]:
    """Builds the mkvmerge command that appends encoded chunks into one video track."""
    if not chunk_paths:
        return []
    cmd = [get_mkvmerge_exe(), '-o', output_video_path, chunk_paths[0]]
    for c in chunk_paths[1:]:
        cmd.extend(['+', c])
    return cmd

def subtitle_flags(title: str, default_audio_is_preferred: bool) -> Tuple[bool, bool]:
    """
    Returns (forced, default) for a subtitle track in the preferred language.
    With preferred-language audio, only signs/songs tracks are shown (forced);
    with other audio, the full subtitle track is shown by default instead.
    """
    t = (title or "").lower()
    is_signs = any(word in t for word in ('sign', 'song', 'forced'))
    if default_audio_is_preferred:
        return (True, True) if is_signs else (False, False)
    return (False, False) if is_signs else (False, True)


def build_mkvmerge_final_mux_cmd(
    output_path: str,
    video_path: str,
    audio_path: Optional[str],
    source_path: str,
    audio_streams: List[Dict[str, Any]],
    default_audio_pos: int,
    selected_subs: List[int],
    sub_streams: List[Dict[str, Any]],
    audio_language: str,
    smart_sub: bool = True,
    custom_sub_titles: Optional[Dict[int, str]] = None
) -> List[str]:
    """Muxes the AV1 video, Opus audio, and selected subtitles/attachments/chapters from the source."""
    cmd = [
        get_mkvmerge_exe(), '-o', output_path,
        '--no-subtitles', '--no-attachments', '--no-chapters',
        '--language', '0:und',
        video_path
    ]

    if audio_path:
        for idx, a in enumerate(audio_streams):
            lang = normalize_language(a.get('language'))
            cmd.extend([
                '--language', f"{idx}:{lang}",
                '--track-name', f"{idx}:{a.get('title') or language_name(lang)}",
                '--default-track-flag', f"{idx}:{'1' if idx == default_audio_pos else '0'}"
            ])
        cmd.append(audio_path)

    cmd.extend(['--no-video', '--no-audio'])
    if not selected_subs:
        cmd.extend(['--no-subtitles', source_path])
        return cmd

    cmd.extend(['--subtitle-tracks', ",".join(str(s) for s in selected_subs)])
    preferred = normalize_language(audio_language)
    default_audio_is_preferred = bool(audio_streams) and         normalize_language(audio_streams[default_audio_pos].get('language')) == preferred

    for s in sub_streams:
        s_idx = s['index']
        if s_idx not in selected_subs:
            continue
        custom_title = (custom_sub_titles or {}).get(s_idx, '').strip()
        title = custom_title or s.get('title', '')
        if title:
            cmd.extend(['--track-name', f"{s_idx}:{title}"])
        if smart_sub and normalize_language(s.get('language')) == preferred:
            forced, default = subtitle_flags(s.get('title', ''), default_audio_is_preferred)
            cmd.extend([
                '--forced-display-flag', f"{s_idx}:{int(forced)}",
                '--default-track-flag', f"{s_idx}:{int(default)}"
            ])

    cmd.append(source_path)
    return cmd
