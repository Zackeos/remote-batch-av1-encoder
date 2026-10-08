import pytest

from core.constants import (
    COLOR_FORMAT_AUTO, COLOR_FORMAT_8BIT_SDR, COLOR_FORMAT_10BIT_HDR,
    RESOLUTION_SOURCE, RESOLUTION_1080P, RESOLUTION_720P, RESOLUTION_4K,
    RATE_CONTROL_CQ, RATE_CONTROL_SIZE
)
from core.encoder import (
    calculate_target_bitrate_k, parse_ffmpeg_time, build_svtav1_params, get_scale_filter,
    build_video_filter_and_colorspace, build_ffmpeg_chunk_encode_cmd,
    build_mkvmerge_chunk_concat_cmd, TIME_PATTERN, SPEED_PATTERN, FPS_PATTERN,
    MIN_VIDEO_BITRATE_K
)


def expected_bitrate(target_mb, duration, audio_k, tracks):
    audio_mb = audio_k * tracks * duration / 8192
    video_mb = max(10.0, target_mb - audio_mb - target_mb * 0.005)
    return max(MIN_VIDEO_BITRATE_K, int(video_mb * 8192 / duration))


@pytest.mark.parametrize("target_mb, duration, audio_k, tracks", [
    (1024, 1440, 96, 2),     # ~24 min episode, 1 GB, dual audio
    (4096, 7200, 128, 1),    # 2 h film, 4 GB
    (500, 600, 64, 3),
    (2048, 2700, 0, 0),      # no audio tracks
])
def test_target_bitrate_matches_formula(target_mb, duration, audio_k, tracks):
    assert calculate_target_bitrate_k(target_mb, duration, audio_k, tracks) == \
        expected_bitrate(target_mb, duration, audio_k, tracks)


def test_target_bitrate_example_value():
    # 1 GB over 24 minutes with two 96 kbps tracks:
    # audio = 96*2*1440/8192 = 33.75 MB, container = 5.12 MB, video = 985.13 MB
    # 985.13 MB * 8192 / 1440 s = 5604 kbps
    assert calculate_target_bitrate_k(1024, 1440, 96, 2) == 5604


def test_target_bitrate_output_size_lands_on_target():
    target_mb, duration, audio_k, tracks = 2048, 3000, 96, 2
    video_k = calculate_target_bitrate_k(target_mb, duration, audio_k, tracks)
    total_mb = (video_k + audio_k * tracks) * duration / 8192 + target_mb * 0.005
    assert total_mb == pytest.approx(target_mb, rel=0.001)


def test_more_audio_leaves_less_video_bitrate():
    one = calculate_target_bitrate_k(1000, 1800, 128, 1)
    three = calculate_target_bitrate_k(1000, 1800, 128, 3)
    assert three < one


def test_target_bitrate_has_floor_when_audio_exceeds_target():
    assert calculate_target_bitrate_k(20, 7200, 320, 4) == MIN_VIDEO_BITRATE_K


@pytest.mark.parametrize("target_mb, duration", [(None, 100), (0, 100), (-5, 100), (500, 0)])
def test_target_bitrate_none_for_invalid_input(target_mb, duration):
    assert calculate_target_bitrate_k(target_mb, duration, 96, 2) is None


@pytest.mark.parametrize("stamp, seconds", [
    ("00:00:00.00", 0.0),
    ("00:01:30.50", 90.5),
    ("01:00:00.00", 3600.0),
    ("02:03:04.25", 7384.25),
])
def test_parse_ffmpeg_time(stamp, seconds):
    assert parse_ffmpeg_time(stamp) == pytest.approx(seconds)


def test_progress_regexes_on_real_ffmpeg_line():
    line = "frame= 1234 fps= 45.6 q=-0.0 size=   10240kB time=00:00:51.47 bitrate=1630.0kbits/s speed=1.88x"
    assert TIME_PATTERN.search(line).group(1) == "00:00:51.47"
    assert FPS_PATTERN.search(line).group(1) == "45.6"
    assert SPEED_PATTERN.search(line).group(1) == "1.88x"


@pytest.mark.parametrize("grain, expected", [
    ("Disabled", "tune=0:lp=8"),
    ("0", "tune=0:lp=8"),
    ("Light (8)", "tune=0:lp=8:film-grain=8:film-grain-denoise=0"),
    ("Medium (16)", "tune=0:lp=8:film-grain=16:film-grain-denoise=1"),
    ("10", "tune=0:lp=8:film-grain=10:film-grain-denoise=1"),
])
def test_build_svtav1_params_grain(grain, expected):
    assert build_svtav1_params(grain) == expected


def test_build_svtav1_params_target_bitrate_and_lp():
    params = build_svtav1_params("Disabled", target_bitrate_k=3000, lp=4)
    assert params == "tune=0:lp=4:rc=1:tbr=3000"


@pytest.mark.parametrize("resolution, expected", [
    (RESOLUTION_SOURCE, None),
    (RESOLUTION_1080P, "scale='min(1920,iw)':-2:flags=lanczos"),
    (RESOLUTION_720P, "scale='min(1280,iw)':-2:flags=lanczos"),
    (RESOLUTION_4K, "scale='min(3840,iw)':-2:flags=lanczos"),
])
def test_get_scale_filter(resolution, expected):
    assert get_scale_filter(resolution) == expected


def test_sdr_source_passes_through_as_10bit():
    vf, signals, pix_fmt, vulkan = build_video_filter_and_colorspace(RESOLUTION_SOURCE, COLOR_FORMAT_AUTO, False)
    assert vf is None
    assert pix_fmt == "yuv420p10le"
    assert "bt709" in signals
    assert vulkan is False


def test_8bit_output():
    _, _, pix_fmt, _ = build_video_filter_and_colorspace(RESOLUTION_SOURCE, COLOR_FORMAT_8BIT_SDR, False)
    assert pix_fmt == "yuv420p"


def test_hdr_source_kept_as_hdr_at_source_resolution():
    vf, signals, pix_fmt, vulkan = build_video_filter_and_colorspace(RESOLUTION_SOURCE, COLOR_FORMAT_AUTO, True)
    assert vf is None
    assert "smpte2084" in signals
    assert vulkan is False


def test_hdr_source_tonemapped_when_downscaled_to_1080p():
    vf, signals, _, vulkan = build_video_filter_and_colorspace(RESOLUTION_1080P, COLOR_FORMAT_AUTO, True)
    assert vf.startswith("libplacebo=tonemapping=auto")
    assert vf.endswith("flags=lanczos")
    assert "bt709" in signals
    assert vulkan is True


def test_forced_hdr10_output():
    _, signals, pix_fmt, _ = build_video_filter_and_colorspace(RESOLUTION_SOURCE, COLOR_FORMAT_10BIT_HDR, False)
    assert "bt2020nc" in signals
    assert pix_fmt == "yuv420p10le"


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_chunk_cmd_cq_mode():
    cmd = build_ffmpeg_chunk_encode_cmd("in.mkv", "out.mkv", 60.0, 30.0, cq="24", preset="6",
                                        color_mode=COLOR_FORMAT_AUTO, film_grain="Disabled")
    assert _arg_after(cmd, "-crf") == "24"
    assert _arg_after(cmd, "-ss") == "60.000"
    assert _arg_after(cmd, "-t") == "30.000"
    assert _arg_after(cmd, "-c:v") == "libsvtav1"
    assert "-b:v" not in cmd
    assert cmd[-4:] == ["-an", "-sn", "-dn", "out.mkv"]


def test_chunk_cmd_whole_file_has_no_seek():
    cmd = build_ffmpeg_chunk_encode_cmd("in.mkv", "out.mkv", 0.0, 0.0, cq="22", preset="6",
                                        color_mode=COLOR_FORMAT_AUTO, film_grain="Disabled")
    assert "-ss" not in cmd and "-t" not in cmd


def test_chunk_cmd_target_size_mode():
    cmd = build_ffmpeg_chunk_encode_cmd("in.mkv", "out.mkv", 0.0, 0.0, cq="22", preset="6",
                                        color_mode=COLOR_FORMAT_AUTO, film_grain="Disabled",
                                        rate_control_mode=RATE_CONTROL_SIZE, target_bitrate_k=4000)
    assert _arg_after(cmd, "-b:v") == "4000k"
    assert "-maxrate" not in cmd  # SVT-AV1 only accepts a max bitrate in CRF mode
    assert "-crf" not in cmd
    assert "rc=1:tbr=4000" in _arg_after(cmd, "-svtav1-params")


def test_chunk_cmd_cq_mode_ignores_bitrate():
    cmd = build_ffmpeg_chunk_encode_cmd("in.mkv", "out.mkv", 0.0, 0.0, cq="22", preset="6",
                                        color_mode=COLOR_FORMAT_AUTO, film_grain="Disabled",
                                        rate_control_mode=RATE_CONTROL_CQ, target_bitrate_k=4000)
    assert "-b:v" not in cmd


def test_concat_cmd_appends_chunks():
    cmd = build_mkvmerge_chunk_concat_cmd("joined.mkv", ["a.mkv", "b.mkv", "c.mkv"])
    assert cmd[1:] == ["-o", "joined.mkv", "a.mkv", "+", "b.mkv", "+", "c.mkv"]


def test_concat_cmd_empty():
    assert build_mkvmerge_chunk_concat_cmd("joined.mkv", []) == []
