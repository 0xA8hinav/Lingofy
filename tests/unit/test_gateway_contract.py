from fastapi.testclient import TestClient

from services.gateway.app import app

client = TestClient(app)


def test_gateway_rejects_over_500MB():
    r = client.post(
        "/v1/sessions/s1/video",
        files={"file": ("big.mp4", b"x")},
        headers={"x-mock-size": str(600 * 1024 * 1024)},
    )
    assert r.status_code in (400, 413)


def test_corrupt_video_returns_400():
    r = client.post(
        "/v1/sessions/s1/video", files={"file": ("bad.mp4", b"notavideo")}
    )
    assert r.status_code == 400
