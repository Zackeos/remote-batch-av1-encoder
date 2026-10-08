import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, fresh_queue):
    monkeypatch.setenv("AV1_INPUT_DIR", str(tmp_path / "in"))
    monkeypatch.setenv("AV1_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("AV1_AUDIO_LANGUAGE", "spa")
    import web_app
    web_app = importlib.reload(web_app)
    with TestClient(web_app.app) as c:
        yield c, web_app, tmp_path


def test_index_and_static_served(client):
    c, _, _ = client
    assert "AV1 Encoder" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200


def test_folder_create_and_list(client):
    c, _, tmp_path = client
    assert c.post("/api/raw/folders/create", json={"folder_name": "Series A"}).status_code == 200
    names = [f["name"] for f in c.get("/api/raw/folders").json()["folders"]]
    assert "Series A" in names


def test_scan_lists_videos(client):
    c, _, tmp_path = client
    (tmp_path / "in").mkdir(exist_ok=True)
    (tmp_path / "in" / "Show.S01E01.mkv").write_bytes(b"0" * 10)
    data = c.post("/api/scan", json={"path": ""}).json()
    assert data["count"] == 1
    assert data["files"][0]["label"] == "S01E01"


@pytest.mark.parametrize("endpoint, body", [
    ("/api/scan", {"path": "../"}),
    ("/api/raw/delete", {"paths": ["../../somefile"]}),
    ("/api/raw/move", {"paths": ["/etc/passwd"], "target_folder": ""}),
    ("/api/queue/add", {"paths": ["x.mkv"], "output_dir": "../../elsewhere"}),
])
def test_paths_outside_library_are_rejected(client, endpoint, body):
    c, _, _ = client
    assert c.post(endpoint, json=body).status_code == 400


def test_queue_add_and_controls(client):
    c, _, tmp_path = client
    (tmp_path / "in").mkdir(exist_ok=True)
    src = tmp_path / "in" / "Show.S01E01.mkv"
    src.write_bytes(b"0" * 10)

    data = c.post("/api/queue/add", json={"paths": [str(src)], "workers": 2}).json()
    assert data["added_count"] == 1
    job_id = data["job_ids"][0]

    assert c.post(f"/api/queue/move/{job_id}/sideways").status_code == 400
    assert c.post("/api/queue/retry/missing").status_code == 404
    assert c.post(f"/api/queue/remove/{job_id}").json()["jobs"] == []


def test_downloads_validate_links(client):
    c, _, _ = client
    assert c.post("/api/downloads", json={"urls": ["ftp://example.com/a.mkv"]}).status_code == 400
    assert c.post("/api/downloads", json={"urls": ["", "# comment"]}).status_code == 400


def test_queue_add_uses_configured_audio_language(client):
    c, _, tmp_path = client
    (tmp_path / "in").mkdir(exist_ok=True)
    src = tmp_path / "in" / "Film.mkv"
    src.write_bytes(b"0" * 10)

    jobs = c.post("/api/queue/add", json={"paths": [str(src)]}).json()["jobs"]
    assert jobs[0]["audio_language"] == "spa"
    jobs = c.post("/api/queue/add", json={"paths": [str(src)], "audio_language": "fr"}).json()["jobs"]
    assert jobs[1]["audio_language"] == "fra"


def test_websocket_sends_init(client):
    c, _, tmp_path = client
    with c.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg["event"] == "init"
    assert msg["paths"]["input_dir"] == str(tmp_path / "in")
    assert "cq" in msg["options"]
    assert msg["defaults"]["audio_language"] == "spa"
