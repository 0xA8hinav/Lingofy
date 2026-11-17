from pathlib import Path

from services.dub_worker.voices import cost_cents, synthesize, translate, voice_for


def test_speaker_voice_stable():
    assert voice_for("1", "hi") == voice_for("1", "hi")
    assert voice_for("1", "hi") != voice_for("2", "hi")


def test_piper_cost_zero():
    assert cost_cents(5000, "piper") == 0.0


def test_mixed_lang_chunk_never_empty(tmp_path):
    out = synthesize(translate("Hello नमस्ते", "en", "hi")["target_text"], "hi", "1", str(tmp_path))
    assert out["provider"] in ("piper", "gemini-tts") and Path(out["mp3_path"]).stat().st_size > 0
