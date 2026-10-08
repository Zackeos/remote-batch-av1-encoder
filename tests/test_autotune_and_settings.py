import json
import os

from core.autotune import recommend_profile
from core.process_utils import format_time_hms
from core.settings import load_settings, PROJECT_ROOT


def result(name, cq, grain, proj_gb, vmaf):
    return {"name": name, "cq": cq, "grain": grain, "proj_gb": proj_gb, "vmaf": vmaf, "clip_size_mb": 1.0}


def test_recommends_smallest_profile_above_vmaf_target():
    results = [
        result("ref", "20", "Disabled", 10.0, 98.0),
        result("mid", "24", "Disabled", 6.0, 95.5),
        result("small", "28", "Disabled", 4.0, 91.0),
    ]
    best = recommend_profile(results)
    assert best["name"] == "mid"
    assert "40%" in best["reason"]


def test_falls_back_to_highest_vmaf_when_none_pass():
    results = [result("a", "20", "Disabled", 10.0, 90.0), result("b", "24", "Disabled", 6.0, 92.0)]
    assert recommend_profile(results)["name"] == "b"


def test_recommend_handles_empty_and_zero_size():
    assert recommend_profile([]) is None
    best = recommend_profile([result("a", "20", "Disabled", 0.0, 97.0)])
    assert best["name"] == "a"


def test_format_time_hms():
    assert format_time_hms(59) == "0:59"
    assert format_time_hms(61) == "1:01"
    assert format_time_hms(3725) == "1:02:05"


def test_settings_defaults(monkeypatch, tmp_path):
    for var in ("AV1_INPUT_DIR", "AV1_OUTPUT_DIR", "AV1_AUDIO_LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings(str(tmp_path / "missing.json"))
    assert s["input_dir"] == os.path.join(PROJECT_ROOT, "downloads", "raw")
    assert s["audio_language"] == "eng"


def test_settings_file_then_env_override(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"input_dir": str(tmp_path / "in"), "audio_language": "jpn", "unknown": 1}))
    monkeypatch.delenv("AV1_INPUT_DIR", raising=False)
    monkeypatch.setenv("AV1_AUDIO_LANGUAGE", "es")

    s = load_settings(str(cfg))
    assert s["input_dir"] == str(tmp_path / "in")
    assert s["audio_language"] == "spa"
    assert "unknown" not in s


def test_settings_language_from_file_is_normalized(monkeypatch, tmp_path):
    monkeypatch.delenv("AV1_AUDIO_LANGUAGE", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"audio_language": "French"}))
    assert load_settings(str(cfg))["audio_language"] == "fra"


def test_settings_ignores_invalid_json(monkeypatch, tmp_path):
    monkeypatch.delenv("AV1_AUDIO_LANGUAGE", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")
    assert load_settings(str(cfg))["audio_language"] == "eng"
