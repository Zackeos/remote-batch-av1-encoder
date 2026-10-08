"""
Filename parsing and episode detection for video files (series, specials, movies).
"""

import os
import re
from typing import Optional, Dict, Any
from .constants import VIDEO_EXTENSIONS

def is_video_file(filename: str) -> bool:
    """Checks if a file extension matches supported video formats."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in VIDEO_EXTENSIONS

def clean_filename(filename: str) -> str:
    """Returns filename stem without extension."""
    stem, _ = os.path.splitext(filename)
    return stem

def _format_episode_result(season: Optional[int], ep_val: Any, ep_end: Optional[int]) -> Dict[str, Any]:
    if isinstance(ep_val, float):
        ep_str = f"{ep_val:04.1f}"
    else:
        ep_str = f"{ep_val:02d}" if isinstance(ep_val, int) and ep_val < 100 else f"{ep_val}"

    if ep_end:
        ep_end_str = f"{ep_end:02d}" if ep_end < 100 else f"{ep_end}"
        ep_display = f"{ep_str}-{ep_end_str}"
    else:
        ep_display = ep_str

    norm_key = f"S{season:02d}E{ep_display}" if season is not None else f"EP{ep_display}"

    return {
        'season': season,
        'episode': ep_val,
        'episode_end': ep_end,
        'special_type': None,
        'normalized_key': norm_key,
        'display_label': norm_key
    }

def extract_episode_info(filename: str) -> Optional[Dict[str, Any]]:
    """
    Extracts season/episode numbers, specials and movies from a filename,
    covering scene, fansub and plain naming conventions.
    """
    stem = clean_filename(filename)

    # 1. Specials / OVA / OAD / NCED / NCOP
    specials_pattern = r'(?i)(?:^|[\s._\-\[(])(OVA|OAD|SP|SPECIAL|NCED|NCOP|EXTRA)\s*[-_.]?\s*(\d{1,3})(?:[\s._\-\])\)]|$)'
    m = re.search(specials_pattern, stem)
    if m:
        sp_type = m.group(1).upper()
        if sp_type == 'SPECIAL':
            sp_type = 'SP'
        sp_num = int(m.group(2))
        norm_key = f"{sp_type}{sp_num:02d}"
        return {
            'season': None,
            'episode': sp_num,
            'episode_end': None,
            'special_type': sp_type,
            'normalized_key': norm_key,
            'display_label': norm_key
        }

    # 2. SxxExx
    sxxexx_pattern = r'(?i)(?:^|[\s._\-\[(])S(\d{1,2})[\s._\-]*(?:E|EP)(\d{1,4})(?:[-_~]*(?:E|EP)?(\d{1,4}))?(?:[\s._\-\])\)]|$)'
    m = re.search(sxxexx_pattern, stem)
    if m:
        season = int(m.group(1))
        ep_start = int(m.group(2))
        ep_end = int(m.group(3)) if m.group(3) else None
        if ep_end:
            norm_key = f"S{season:02d}E{ep_start:02d}-E{ep_end:02d}"
            ep_val = f"{ep_start}-{ep_end}"
        else:
            norm_key = f"S{season:02d}E{ep_start:02d}"
            ep_val = ep_start
        return {
            'season': season,
            'episode': ep_val,
            'episode_end': ep_end,
            'special_type': None,
            'normalized_key': norm_key,
            'display_label': norm_key
        }

    # 3. 1x02 or 01x02
    x_pattern = r'(?i)(?:^|[\s._\-\[(])(\d{1,2})x(\d{1,4})(?:[-_~xX](\d{1,4}))?(?:[\s._\-\])\)]|$)'
    m = re.search(x_pattern, stem)
    if m:
        season = int(m.group(1))
        ep_start = int(m.group(2))
        ep_end = int(m.group(3)) if m.group(3) else None
        if ep_end:
            norm_key = f"S{season:02d}E{ep_start:02d}-E{ep_end:02d}"
            ep_val = f"{ep_start}-{ep_end}"
        else:
            norm_key = f"S{season:02d}E{ep_start:02d}"
            ep_val = ep_start
        return {
            'season': season,
            'episode': ep_val,
            'episode_end': ep_end,
            'special_type': None,
            'normalized_key': norm_key,
            'display_label': norm_key
        }

    # 4. Season X ... Episode Y
    season_ep_pattern = r'(?i)season\s*(\d{1,2})[\s._\-]+(?:episode|ep|e)?\s*(\d{1,4})'
    m = re.search(season_ep_pattern, stem)
    if m:
        season = int(m.group(1))
        ep_start = int(m.group(2))
        norm_key = f"S{season:02d}E{ep_start:02d}"
        return {
            'season': season,
            'episode': ep_start,
            'episode_end': None,
            'special_type': None,
            'normalized_key': norm_key,
            'display_label': norm_key
        }

    season_found = None
    m_season = re.search(r'(?i)(?:^|[\s._\-\[(])(?:season|s)\s*(\d{1,2})(?:[\s._\-\])\)]|$)', stem)
    if m_season:
        season_found = int(m_season.group(1))

    clean_stem = stem
    clean_stem = re.sub(r'\[(?:1080p|720p|480p|2160p|4k|x264|x265|h264|hevc|av1|10bit|8bit|aac|flac|dts|ac3|eac3|dual[\s\-_]audio|multi[\s\-_]sub|[0-9a-f]{8})[^\]]*\]', ' ', clean_stem, flags=re.IGNORECASE)
    clean_stem = re.sub(r'\[(?:19\d{2}|20\d{2})\]', ' ', clean_stem)
    clean_stem = re.sub(r'\((?:1080p|720p|480p|2160p|4k|x264|x265|h264|hevc|av1|10bit|8bit|19\d{2}|20\d{2})[^\)]*\)', ' ', clean_stem, flags=re.IGNORECASE)

    # 5. Episode / Ep
    ep_prefix_pattern = r'(?i)(?:^|[\s._\-\[(])(?:episode|episodes|ep|eps)\.?\s*(\d{1,4}(?:\.5)?)(?:[-_~](?:ep|eps)?\.?\s*(\d{1,4}))?(?:v\d+)?(?:[\s._\-\])\)]|$)'
    m = re.search(ep_prefix_pattern, clean_stem)
    if m:
        ep_raw = m.group(1)
        ep_end = int(m.group(2)) if m.group(2) else None
        ep_val = float(ep_raw) if '.' in ep_raw else int(ep_raw)
        return _format_episode_result(season_found, ep_val, ep_end)

    # 6. E05
    e_pattern = r'(?i)(?:^|[\s._\-\[(])E(\d{1,4})(?:v\d+)?(?:[\s._\-\])\)]|$)'
    m = re.search(e_pattern, clean_stem)
    if m:
        ep_val = int(m.group(1))
        return _format_episode_result(season_found, ep_val, None)

    # 7. Fansub-style release: "Title - 05"
    stripped_group = re.sub(r'^\s*\[[^\]]+\]\s*', '', clean_stem)
    norm_space = re.sub(r'_+', ' ', stripped_group)

    dash_ep_pattern = r'(?:^|\s)[\-_–—]\s*(\d{1,4}(?:\.5)?)(?:v\d+)?(?:\s*[\-_–—]|\s*\[|\s*\(|\s*$)'
    m = re.search(dash_ep_pattern, norm_space)
    if m:
        ep_raw = m.group(1)
        ep_val = float(ep_raw) if '.' in ep_raw else int(ep_raw)
        return _format_episode_result(season_found, ep_val, None)

    # Dot-separated releases: e.g. "Show.01.1080p"
    dot_matches = list(re.finditer(r'\.(\d{1,3})\.', stripped_group))
    if dot_matches:
        ep_val = int(dot_matches[-1].group(1))
        return _format_episode_result(season_found, ep_val, None)

    # 8. Standalone Movies / Films / Videos
    year_match = re.search(r'(?:^|[\s._\-\[(])(19\d{2}|20\d{2})(?:[\s._\-\])\)]|$)', stem)
    is_movie = bool(re.search(r'(?i)(?:movie|film|theatrical)', stem) or year_match)
    if is_movie:
        year_str = f" ({year_match.group(1)})" if year_match else ""
        return {
            'season': None,
            'episode': None,
            'episode_end': None,
            'special_type': 'MOVIE',
            'normalized_key': 'MOVIE',
            'display_label': f"Movie{year_str}"
        }

    return {
        'season': None,
        'episode': None,
        'episode_end': None,
        'special_type': 'VIDEO',
        'normalized_key': 'VIDEO',
        'display_label': 'Video'
    }

def _episode_sort_value(episode: Any) -> float:
    """Numeric sort value for an episode; ranges like "1-2" sort by their first number."""
    if isinstance(episode, (int, float)):
        return episode
    if isinstance(episode, str):
        m = re.match(r'\d+', episode)
        if m:
            return int(m.group(0))
    return 9999


def scan_video_files(src_dir: str, recursive: bool = False):
    """
    Scans a directory for video files, extracts episode info, and sorts them in viewing order.
    Returns: List of tuples (rel_dir, season_num, ep_num, full_path, display_label)
    """
    if not os.path.exists(src_dir):
        return []

    collected = []
    if recursive:
        for root, _, files in os.walk(src_dir):
            rel = os.path.relpath(root, src_dir)
            for f in files:
                if is_video_file(f):
                    collected.append((rel, os.path.join(root, f)))
    else:
        for f in os.listdir(src_dir):
            full_p = os.path.join(src_dir, f)
            if os.path.isfile(full_p) and is_video_file(f):
                collected.append((".", full_p))

    parsed = []
    for rel_dir, f_path in collected:
        fname = os.path.basename(f_path)
        info = extract_episode_info(fname)
        s_val = info.get('season')
        e_val = info.get('episode')
        label = info.get('display_label', 'Video')
        parsed.append((rel_dir, s_val, e_val, f_path, label))

    def sort_key(item):
        r_dir, s_num, e_num, f_path, _ = item
        s_score = s_num if s_num is not None else 9999
        e_score = _episode_sort_value(e_num)
        return (r_dir, s_score, e_score, os.path.basename(f_path).lower())

    return sorted(parsed, key=sort_key)
