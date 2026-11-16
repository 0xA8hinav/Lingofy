from fastapi.testclient import TestClient

from services.dub_worker.diarize import cluster_session
from services.gateway.app import app

client = TestClient(app)


def test_cluster_generic_no_hardcode():
    assert cluster_session([[5.0], [1.0], [3.0], [2.0]]) != [1, 1, 2, 2, 3, 3]
    assert max(cluster_session([[5.0], [1.0], [3.0], [2.0]])) <= 3


def test_video_needs_ffmpeg_or_chunks():
    import shutil

    if shutil.which("ffmpeg") is None:
        r = client.post("/v1/sessions/s2/video", files={"file": ("c.mp4", b"\x00\x00\x00\x20ftyp" + b"x" * 2000)})
        assert r.status_code == 400
        assert "ffmpeg" in r.text.lower()
