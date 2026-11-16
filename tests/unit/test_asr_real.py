from services.dub_worker.worker import transcribe


def test_transcribe_real_tone():
    r = transcribe("tests/fixtures/tone_16s.wav", "en")
    assert r["provider"] in ("faster-whisper", "deepgram", "mock")
    assert "text" in r and "confidence" in r
