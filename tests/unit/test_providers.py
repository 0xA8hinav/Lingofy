from services.dub_worker.providers import DumbBreaker, call_with_retry


def test_retry_jitter_bounds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            e = RuntimeError("429 rate limited")
            e.status = 429
            raise e
        return "ok"

    assert call_with_retry(flaky, retries=2) == "ok"
    assert calls["n"] == 3


def test_dumb_breaker_trips_after_3():
    b = DumbBreaker(failures=3, fallback_s=60)
    for _ in range(3):
        b.record_failure("deepgram")
    assert b.use_fallback("deepgram") is True
