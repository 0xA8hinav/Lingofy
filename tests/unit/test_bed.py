import wave

from services.dub_worker.bed import dubbed_with_background, speech_mask
from services.dub_worker.voices import synthesize


def _write_wav(path, samples, sr=16000):
    import struct

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<" + "h" * len(samples), *samples))


def test_bed_ducks_speech_keeps_ambience(tmp_path):
    sr = 16000
    # 1s loud tone (speech), 1s quiet tone (ambience), 1s loud tone
    loud = [(12000 * (1 if i % 2 else -1)) for i in range(sr)]
    quiet = [(1200 * (1 if i % 2 else -1)) for i in range(sr)]
    src = str(tmp_path / "chunk.wav")
    _write_wav(src, loud + quiet + loud, sr)
    mask = speech_mask(loud + quiet + loud, sr)
    assert mask[5] is True
    assert any(m is False for m in mask)

    voice = synthesize("नमस्ते", "hi", "1", str(tmp_path), name="v", target_ms=3000)
    out = dubbed_with_background(src, voice["wav_path"], str(tmp_path), "mix")
    with wave.open(out, "rb") as w:
        assert w.getnframes() > 0
