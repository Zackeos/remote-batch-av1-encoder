import pytest

from core.constants import (
    RATE_CONTROL_SIZE, RATE_CONTROL_CQ, AUDIO_TRACKS_PREFERRED, AUDIO_TRACKS_PREFERRED_AND_ORIGINAL,
    AUDIO_TRACKS_ORIGINAL_AND_PREFERRED, AUDIO_TRACKS_FIRST, AUDIO_TRACKS_ALL
)
from core.job import Job, STATUS_PENDING, STATUS_COMPLETED, STATUS_FAILED, STATUS_SKIPPED
from core.queue_manager import QueueManager, select_audio_streams

JPN = {"index": 1, "language": "jpn", "channels": 2}
ENG = {"index": 2, "language": "eng", "channels": 6}
SPA = {"index": 3, "language": "spa", "channels": 2}
FR2 = {"index": 4, "language": "fr", "channels": 2}  # two-letter code in the container


def make_job(tmp_path, name="a.mkv", **kwargs):
    return Job(source_path=str(tmp_path / name), output_dir=str(tmp_path / "out"), **kwargs)


@pytest.mark.parametrize("tracks, language, streams, expected, default_pos", [
    # Preferred language only, falling back to the original (first) track
    (AUDIO_TRACKS_PREFERRED, "eng", [JPN, ENG], [ENG], 0),
    (AUDIO_TRACKS_PREFERRED, "spa", [JPN, ENG, SPA], [SPA], 0),
    (AUDIO_TRACKS_PREFERRED, "deu", [JPN, ENG], [JPN], 0),
    (AUDIO_TRACKS_PREFERRED, "fra", [ENG, FR2], [FR2], 0),      # "fr" matches "fra"
    (AUDIO_TRACKS_PREFERRED, "en", [JPN, ENG], [ENG], 0),       # "en" matches "eng"
    # Preferred + original, preferred first and default
    (AUDIO_TRACKS_PREFERRED_AND_ORIGINAL, "eng", [JPN, ENG, SPA], [ENG, JPN], 0),
    (AUDIO_TRACKS_PREFERRED_AND_ORIGINAL, "spa", [SPA, ENG], [SPA], 0),  # preferred is the original
    (AUDIO_TRACKS_PREFERRED_AND_ORIGINAL, "deu", [JPN, ENG], [JPN], 0),  # no match
    # Original + preferred, original first and default
    (AUDIO_TRACKS_ORIGINAL_AND_PREFERRED, "eng", [JPN, ENG], [JPN, ENG], 0),
    # First track only ignores the language
    (AUDIO_TRACKS_FIRST, "eng", [SPA, ENG], [SPA], 0),
    # All tracks; the preferred one is the default
    (AUDIO_TRACKS_ALL, "eng", [JPN, ENG, SPA], [JPN, ENG, SPA], 1),
    (AUDIO_TRACKS_ALL, "deu", [JPN, ENG], [JPN, ENG], 0),
    # No audio at all
    (AUDIO_TRACKS_PREFERRED, "eng", [], [], 0),
])
def test_select_audio_streams(tracks, language, streams, expected, default_pos):
    assert select_audio_streams(tracks, language, streams) == (expected, default_pos)


def test_queue_manager_is_singleton(fresh_queue):
    assert QueueManager() is fresh_queue


def test_add_move_and_remove_jobs(tmp_path, fresh_queue):
    a, b, c = (make_job(tmp_path, n) for n in ("a.mkv", "b.mkv", "c.mkv"))
    fresh_queue.add_jobs([a, b, c])

    assert fresh_queue.move_job(c.id, "up")
    assert [j.id for j in fresh_queue.jobs] == [a.id, c.id, b.id]
    assert not fresh_queue.move_job(a.id, "up")  # already first
    assert not fresh_queue.move_job("missing", "down")

    assert fresh_queue.remove_job(a.id)
    assert [j.id for j in fresh_queue.jobs] == [c.id, b.id]
    assert not fresh_queue.remove_job(a.id)


def test_clear_completed_keeps_unfinished(tmp_path, fresh_queue):
    done, failed, pending = (make_job(tmp_path, n) for n in ("a.mkv", "b.mkv", "c.mkv"))
    done.status, failed.status = STATUS_COMPLETED, STATUS_FAILED
    fresh_queue.add_jobs([done, failed, pending])

    fresh_queue.clear_completed()
    assert fresh_queue.jobs == [pending]


def test_events_are_emitted(tmp_path, fresh_queue):
    events = []
    fresh_queue.add_listener(lambda kind, data: events.append(kind))
    fresh_queue.add_job(make_job(tmp_path))
    assert events == ["job_added", "queue_updated"]


def test_skip_existing_output(tmp_path, fresh_queue):
    (tmp_path / "out").mkdir()
    (tmp_path / "a.mkv").write_bytes(b"x" * 1024)
    (tmp_path / "out" / "a.mkv").write_bytes(b"x" * (11 * 1024 * 1024))
    job = make_job(tmp_path)

    assert fresh_queue._execute_job(job) is True
    assert job.status == STATUS_SKIPPED


def test_target_bitrate_only_in_size_mode(tmp_path, fresh_queue):
    size_job = make_job(tmp_path, rate_control_mode=RATE_CONTROL_SIZE, target_size_mb=1024, audio_bitrate="96")
    cq_job = make_job(tmp_path, rate_control_mode=RATE_CONTROL_CQ, target_size_mb=1024)
    assert fresh_queue._target_bitrate_k(size_job, 1440, 2) == 5604
    assert fresh_queue._target_bitrate_k(cq_job, 1440, 2) is None


def test_retry_resets_failed_job(tmp_path, fresh_queue, monkeypatch):
    monkeypatch.setattr(fresh_queue, "start_queue", lambda: None)
    job = make_job(tmp_path)
    job.status, job.error_message, job.progress = STATUS_FAILED, "boom", 0.4
    fresh_queue.add_job(job)

    fresh_queue.retry_job(job.id)
    assert (job.status, job.error_message, job.progress) == (STATUS_PENDING, "", 0.0)


def test_job_defaults_and_output_path(tmp_path):
    job = make_job(tmp_path, "Show.S01E01.mp4")
    assert job.stem == "Show.S01E01"
    assert job.title == "Show.S01E01.mp4"
    assert job.final_output_path.endswith("Show.S01E01.mkv")
    assert job.to_dict()["status"] == STATUS_PENDING
