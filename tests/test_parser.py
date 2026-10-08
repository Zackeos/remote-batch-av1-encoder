import os

import pytest

from core.parser import extract_episode_info, scan_video_files, is_video_file, clean_filename


@pytest.mark.parametrize("filename, key, season, episode, special", [
    # SxxExx
    ("Show.Name.S01E05.1080p.WEB-DL.mkv", "S01E05", 1, 5, None),
    ("Show Name - S02E10 - Title.mkv", "S02E10", 2, 10, None),
    ("show_name_s1e3.mp4", "S01E03", 1, 3, None),
    # Multi-episode files
    ("Show.S01E01-E02.1080p.mkv", "S01E01-E02", 1, "1-2", None),
    ("Show.S01E01E02.mkv", "S01E01-E02", 1, "1-2", None),
    # 1x07 style
    ("Show 1x07 Title.avi", "S01E07", 1, 7, None),
    # Spelled out
    ("Show Season 2 Episode 4.mkv", "S02E04", 2, 4, None),
    ("Show Season 2 - 03 [1080p].mkv", "S02E03", 2, 3, None),
    # Fansub "Title - 05" with group tag, hash and version suffix
    ("[SubGroup] Show Name - 05 [1080p][ABCD1234].mkv", "EP05", None, 5, None),
    ("[SubGroup] Show Name - 12v2 [720p].mkv", "EP12", None, 12, None),
    ("[Group] Show - 07.5 [1080p].mkv", "EP07.5", None, 7.5, None),
    # Episode / Ep / E prefixes
    ("Show Name Episode 11.mkv", "EP11", None, 11, None),
    ("Show Name Ep.03.mkv", "EP03", None, 3, None),
    ("Show Name E09.mkv", "EP09", None, 9, None),
    # Dot separated
    ("Show.Name.07.1080p.mkv", "EP07", None, 7, None),
    # Specials
    ("[Group] Show Name OVA 2 [1080p].mkv", "OVA02", None, 2, "OVA"),
    ("Show NCOP 01.mkv", "NCOP01", None, 1, "NCOP"),
    ("Show Special 3.mkv", "SP03", None, 3, "SP"),
    # Movies and unrecognised videos
    ("Some Film (2019) 1080p.mkv", "MOVIE", None, None, "MOVIE"),
    ("The Movie.mkv", "MOVIE", None, None, "MOVIE"),
    ("home_video.mov", "VIDEO", None, None, "VIDEO"),
])
def test_extract_episode_info(filename, key, season, episode, special):
    info = extract_episode_info(filename)
    assert info["normalized_key"] == key
    assert info["season"] == season
    assert info["episode"] == episode
    assert info["special_type"] == special


def test_movie_label_includes_year():
    assert extract_episode_info("Some Film (2019) 1080p.mkv")["display_label"] == "Movie (2019)"


def test_multi_episode_records_end():
    assert extract_episode_info("Show.S01E01-E02.mkv")["episode_end"] == 2


@pytest.mark.parametrize("filename, expected", [
    ("a.mkv", True),
    ("a.MP4", True),
    ("a.webm", True),
    ("a.srt", False),
    ("a.mkv.part", False),
    ("noext", False),
])
def test_is_video_file(filename, expected):
    assert is_video_file(filename) is expected


def test_clean_filename_strips_only_last_extension():
    assert clean_filename("Show.S01E01.mkv") == "Show.S01E01"


def _touch(path):
    with open(path, "wb"):
        pass


def test_scan_sorts_by_season_then_episode(tmp_path):
    for name in ["Show.S02E01.mkv", "Show.S01E10.mkv", "Show.S01E02.mkv", "notes.txt"]:
        _touch(tmp_path / name)

    names = [os.path.basename(item[3]) for item in scan_video_files(str(tmp_path))]
    assert names == ["Show.S01E02.mkv", "Show.S01E10.mkv", "Show.S02E01.mkv"]


def test_scan_handles_mixed_single_and_multi_episode_files(tmp_path):
    # Multi-episode files store the episode as a string ("2-3"); sorting must not mix str and int.
    for name in ["Show.S01E04.mkv", "Show.S01E02-E03.mkv", "Show.S01E01.mkv"]:
        _touch(tmp_path / name)

    names = [os.path.basename(item[3]) for item in scan_video_files(str(tmp_path))]
    assert names == ["Show.S01E01.mkv", "Show.S01E02-E03.mkv", "Show.S01E04.mkv"]


def test_scan_recursive_keeps_relative_dirs(tmp_path):
    (tmp_path / "Season 1").mkdir()
    _touch(tmp_path / "Season 1" / "Show.S01E01.mkv")
    _touch(tmp_path / "Show.S00E01.mkv")

    flat = scan_video_files(str(tmp_path))
    deep = scan_video_files(str(tmp_path), recursive=True)
    assert len(flat) == 1
    assert sorted(item[0] for item in deep) == [".", "Season 1"]


def test_scan_missing_folder_returns_empty():
    assert scan_video_files("/definitely/not/a/real/folder") == []
