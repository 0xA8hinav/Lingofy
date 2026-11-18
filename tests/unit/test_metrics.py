from fastapi.testclient import TestClient

from services.gateway.app import app

client = TestClient(app)


def test_metrics_endpoint_prometheus_format():
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    for name in (
        "lingofy_sessions_total",
        "lingofy_chunks_done_total",
        "lingofy_chirp_calls_total",
        "lingofy_chirp_minutes_total",
    ):
        assert name in body
