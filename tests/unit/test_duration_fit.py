import wave

from services.dub_worker.voices import synthesize


def _dur(p):
    with wave.open(p, "rb") as w:
        return w.getnframes() / w.getframerate()


def test_tts_matches_source_duration(tmp_path):
    src = "tests/fixtures/tone_16s.wav"
    with wave.open(src, "rb") as w:
        target_ms = int(w.getnframes() / w.getframerate() * 1000)
    out = synthesize("नमस्ते दोस्तों, यह एक परीक्षण है", "hi", "1", str(tmp_path), target_ms=target_ms)
    got_ms = int(_dur(out["wav_path"]) * 1000)
    assert abs(got_ms - target_ms) <= max(1000, target_ms * 0.15)
