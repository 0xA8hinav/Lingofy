"""Worker: transcribe via Gemini diarization > Groq > faster-whisper > mock. $0 offline default."""

import json
import os
from pathlib import Path


def poll_once(queue_dir: str) -> dict | None:
    q = Path(queue_dir)
    if not q.exists():
        return None
    for f in sorted(q.glob("*.json")):
        msg = json.loads(f.read_text())
        f.unlink()
        return msg
    return None


def transcribe_groq(wav_path: str, source_lang: str) -> dict:
    """Groq whisper-large-v3: free, word+segment timestamps, NO diarization.

    Translations endpoint is English-only; diarization comes from Gemini
    or the pitch fallback. Needs GROQ_API_KEY.
    """
    import os

    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    lang = (source_lang or "en")[:2]
    with open(wav_path, "rb") as f:
        tx = client.audio.transcriptions.create(
            file=(Path(wav_path).name, f.read()),
            model="whisper-large-v3",
            language=lang,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
            temperature=0.0,
        )
    words = [{"word": w.get("word"), "start": w.get("start"), "end": w.get("end")} for w in (tx.words or [])]
    text = (tx.text or "").strip()
    if not text:
        raise ValueError("empty groq transcript")
    try:
        import wave as _w

        with _w.open(wav_path, "rb") as _f:
            _s = _f.getnframes() / (_f.getframerate() or 1)
        from services.dub_worker import costs as _c

        _c.record("groq", _s, Path(wav_path).name)
    except Exception:
        pass
    return {"text": text, "confidence": 0.9, "provider": "groq", "words": words}


def transcribe(wav_path: str, source_lang: str) -> dict:
    if os.getenv("GROQ_API_KEY"):
        try:
            return transcribe_groq(wav_path, source_lang)
        except Exception:
            pass
    try:
        from faster_whisper import WhisperModel

        global _FW_MODEL
        if "_FW_MODEL" not in globals():
            _FW_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
        segs, _info = _FW_MODEL.transcribe(wav_path, language=source_lang, beam_size=1)
        text = " ".join(s.text.strip() for s in segs).strip()
        if not text:
            raise ValueError("empty transcript")
        return {"text": text, "confidence": 0.8, "provider": "faster-whisper"}
    except Exception:
        return {"text": f"mock transcript {Path(wav_path).name} {source_lang}", "confidence": 1.0, "provider": "mock"}
