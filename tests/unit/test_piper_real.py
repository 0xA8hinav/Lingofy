import wave

from services.dub_worker.voices import synthesize


def test_piper_real_wav(tmp_path):
    out = synthesize("नमस्ते", "hi", "1", str(tmp_path))
    assert out["provider"] in ("piper", "gemini-tts")
    with wave.open(out["wav_path"], "rb") as w:
        assert w.getnframes() > 1000
        assert w.getframerate() == 22050
