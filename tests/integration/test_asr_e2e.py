from services.dub_worker.worker import transcribe


def test_transcribe_offline_mock():
    r = transcribe("tests/fixtures/tone_16s.wav", "en")
    assert r["text"] and r["provider"] in ("deepgram", "mock")
