import pytest

from core.constants import NORM_MODE_OPTIONS, AUDIO_CHANNELS_OPTIONS, normalize_language, language_name
from core.encoder import (
    build_audio_filter, build_ffmpeg_audio_encode_cmd, build_mkvmerge_final_mux_cmd,
    norm_target_lufs, subtitle_flags
)

STEREO_LAYOUT, SURROUND_LAYOUT = AUDIO_CHANNELS_OPTIONS
TWO_PASS, DYNAMIC_20, DYNAMIC_23, NORM_OFF = NORM_MODE_OPTIONS


@pytest.mark.parametrize("code, expected", [
    ("eng", "eng"), ("en", "eng"), ("English", "eng"), (" JA ", "jpn"),
    ("fre", "fra"), ("ger", "deu"), ("", "und"), (None, "und"), ("tlh", "tlh"),
])
def test_normalize_language(code, expected):
    assert normalize_language(code) == expected


def test_language_name():
    assert language_name("es") == "Spanish"
    assert language_name("tlh") == "TLH"


@pytest.mark.parametrize("norm, expected", [
    (TWO_PASS, -20.0), (DYNAMIC_20, -20.0), (DYNAMIC_23, -23.0), (NORM_OFF, None),
])
def test_norm_target_lufs(norm, expected):
    assert norm_target_lufs(norm) == expected


@pytest.mark.parametrize("channels, layout, norm, expected", [
    (2, STEREO_LAYOUT, NORM_OFF, "aformat=channel_layouts=stereo"),
    (6, STEREO_LAYOUT, NORM_OFF, "aformat=channel_layouts=stereo"),
    (6, SURROUND_LAYOUT, NORM_OFF, None),
    (2, SURROUND_LAYOUT, NORM_OFF, "aformat=channel_layouts=stereo"),
    (2, STEREO_LAYOUT, DYNAMIC_23, "aformat=channel_layouts=stereo,loudnorm=I=-23.0:TP=-1.5:LRA=11.0"),
    (6, SURROUND_LAYOUT, DYNAMIC_20, "loudnorm=I=-20.0:TP=-1.5:LRA=11.0"),
])
def test_build_audio_filter(channels, layout, norm, expected):
    assert build_audio_filter({"channels": channels}, layout, norm) == expected


def test_two_pass_filter_uses_measurements():
    measured = {"input_i": "-30.1", "input_tp": "-4.0", "input_lra": "7.5", "input_thresh": "-40.2"}
    af = build_audio_filter({"channels": 2}, STEREO_LAYOUT, TWO_PASS, measured)
    assert af.endswith("measured_I=-30.1:measured_TP=-4.0:measured_LRA=7.5:measured_thresh=-40.2:linear=true")


def test_audio_encode_cmd_maps_each_track():
    cmd = build_ffmpeg_audio_encode_cmd("in.mkv", "a.mkv", "96", [{"index": 2}, {"index": 1}], ["af1", None])
    maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
    assert maps == ["0:2", "0:1"]
    assert cmd[cmd.index("-filter:a:0") + 1] == "af1"
    assert "-filter:a:1" not in cmd
    assert cmd[cmd.index("-b:a:1") + 1] == "96k"
    assert cmd[-1] == "a.mkv"


@pytest.mark.parametrize("title, audio_is_preferred, expected", [
    ("Signs & Songs", True, (True, True)),
    ("Full Subtitles", True, (False, False)),
    ("Forced", True, (True, True)),
    ("Signs & Songs", False, (False, False)),
    ("Full Subtitles", False, (False, True)),
    ("", False, (False, True)),
])
def test_subtitle_flags(title, audio_is_preferred, expected):
    assert subtitle_flags(title, audio_is_preferred) == expected


def test_final_mux_cmd_tracks_and_flags():
    audio = [{"index": 2, "language": "en", "title": ""}, {"index": 1, "language": "jpn", "title": "Original"}]
    subs = [
        {"index": 3, "language": "eng", "title": "Signs"},
        {"index": 4, "language": "eng", "title": "Full"},
        {"index": 5, "language": "spa", "title": ""},
    ]
    cmd = build_mkvmerge_final_mux_cmd("out.mkv", "v.mkv", "a.mkv", "src.mkv", audio, 0,
                                       [3, 4, 5], subs, "eng", custom_sub_titles={5: "Spanish"})
    text = " ".join(cmd)
    assert "--language 0:eng --track-name 0:English --default-track-flag 0:1" in text
    assert "--language 1:jpn --track-name 1:Original --default-track-flag 1:0" in text
    assert "--subtitle-tracks 3,4,5" in text
    assert "--forced-display-flag 3:1 --default-track-flag 3:1" in text
    assert "--forced-display-flag 4:0 --default-track-flag 4:0" in text
    assert "--track-name 5:Spanish" in text
    assert "--forced-display-flag 5:" not in text
    assert cmd[-1] == "src.mkv"


def test_final_mux_with_original_audio_default_shows_full_subs():
    audio = [{"index": 1, "language": "jpn", "title": ""}, {"index": 2, "language": "eng", "title": ""}]
    subs = [{"index": 3, "language": "eng", "title": "Full"}]
    text = " ".join(build_mkvmerge_final_mux_cmd("o.mkv", "v.mkv", "a.mkv", "s.mkv", audio, 0, [3], subs, "eng"))
    assert "--forced-display-flag 3:0 --default-track-flag 3:1" in text


def test_final_mux_smart_flags_off():
    subs = [{"index": 3, "language": "eng", "title": "Signs"}]
    audio = [{"index": 1, "language": "eng", "title": ""}]
    cmd = build_mkvmerge_final_mux_cmd("o.mkv", "v.mkv", "a.mkv", "s.mkv", audio, 0, [3], subs, "eng", smart_sub=False)
    assert "--forced-display-flag" not in cmd


def test_final_mux_without_audio_or_subs():
    cmd = build_mkvmerge_final_mux_cmd("out.mkv", "v.mkv", None, "src.mkv", [], 0, [], [], "eng")
    assert "a.mkv" not in cmd
    assert cmd[-2:] == ["--no-subtitles", "src.mkv"]
