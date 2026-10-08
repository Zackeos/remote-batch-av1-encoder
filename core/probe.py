"""
Media inspection and audio loudness analysis utilities using ffprobe and ffmpeg.
"""

import json
import logging
import os
import re
import subprocess
from typing import Optional, Dict, Any
from .constants import DEFAULT_TARGET_LUFS, DEFAULT_TRUE_PEAK, DEFAULT_LRA
from .encoder import get_ffmpeg_exe, get_ffprobe_exe

logger = logging.getLogger(__name__)

def probe_file_details(file_path: str) -> Dict[str, Any]:
    """Returns duration, HDR/color info, and the video, audio and subtitle streams of a file ({} on failure)."""
    cmd = [
        get_ffprobe_exe(), '-v', 'error',
        '-show_entries', 'format=duration:stream=index,codec_name,codec_type,channels,width,height,color_space,color_primaries,color_transfer,pix_fmt:stream_tags=language,title',
        '-of', 'json',
        file_path
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        if res.returncode != 0:
            return {}
        data = json.loads(res.stdout)

        duration = float(data.get('format', {}).get('duration', 0.0))
        streams = data.get('streams', [])

        video_streams = []
        audio_streams = []
        sub_streams = []
        is_hdr = False
        color_space = "bt709"
        color_primaries = "bt709"
        color_transfer = "bt709"

        for s in streams:
            st_type = s.get('codec_type')
            idx = s.get('index')
            codec = s.get('codec_name', '')
            tags = s.get('tags', {}) or {}
            lang = tags.get('language', 'und').lower()
            title = tags.get('title', '')

            if st_type == 'video':
                c_space = s.get('color_space') or ''
                c_prim = s.get('color_primaries') or ''
                c_trc = s.get('color_transfer') or ''
                v_is_hdr = (c_prim == 'bt2020' or c_trc in ('smpte2084', 'arib-std-b67') or '2020' in c_space)
                if v_is_hdr:
                    is_hdr = True
                    color_space = c_space or 'bt2020nc'
                    color_primaries = c_prim or 'bt2020'
                    color_transfer = c_trc or 'smpte2084'
                video_streams.append({
                    'index': idx,
                    'codec': codec,
                    'title': title,
                    'width': s.get('width'),
                    'height': s.get('height'),
                    'is_hdr': v_is_hdr,
                    'color_space': c_space,
                    'color_primaries': c_prim,
                    'color_transfer': c_trc
                })
            elif st_type == 'audio':
                channels = s.get('channels', 2)
                ch_label = f"{channels}.0" if channels <= 2 else "5.1" if channels == 6 else f"{channels}ch"
                label = f"Stream #{idx} [{lang.upper()}] {title or codec} ({ch_label})"
                audio_streams.append({
                    'index': idx,
                    'codec': codec,
                    'language': lang,
                    'title': title,
                    'channels': channels,
                    'label': label
                })
            elif st_type == 'subtitle':
                label = f"Stream #{idx} [{lang.upper()}] {title or codec} ({codec})"
                sub_streams.append({
                    'index': idx,
                    'codec': codec,
                    'language': lang,
                    'title': title,
                    'label': label
                })

        return {
            'duration': duration,
            'is_hdr': is_hdr,
            'color_space': color_space,
            'color_primaries': color_primaries,
            'color_transfer': color_transfer,
            'video': video_streams,
            'audio': audio_streams,
            'subtitles': sub_streams
        }
    except Exception as e:
        logger.warning("ffprobe failed for %s: %s", file_path, e)
        return {}

def measure_audio_loudness(file_path: str, stream_idx: int, target_lufs: float = DEFAULT_TARGET_LUFS, downmix: bool = True) -> Optional[Dict[str, Any]]:
    """First pass of EBU R128 normalization: measures integrated loudness, true peak and LRA."""
    cmd = [
        get_ffmpeg_exe(), '-v', 'info', '-nostats',
        '-i', file_path,
        '-map', f'0:{stream_idx}',
        '-af', f'{"aformat=channel_layouts=stereo," if downmix else ""}loudnorm=I={target_lufs}:TP={DEFAULT_TRUE_PEAK}:LRA={DEFAULT_LRA}:print_format=json',
        '-f', 'null', '-'
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', res.stderr, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        logger.warning("Loudness measurement failed for %s: %s", file_path, e)
    return None
