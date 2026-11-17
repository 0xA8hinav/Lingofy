"""Worker: transcribe via Chirp (diarized) > faster-whisper > mock. $0 offline default."""
import json
import os
from pathlib import Path


def transcribe_chirp(wav_path: str, source_lang: str, max_speakers: int = 3) -> dict:
    """Google Chirp with speaker diarization + word timestamps (OSS client).

    Needs: Speech-to-Text API enabled, GOOGLE_CLOUD_PROJECT set,
    GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account JSON.
    Region via GOOGLE_CLOUD_REGION (default us-central1).
    """
    from google.cloud import speech_v2 as speech

    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.getenv("GOOGLE_CLOUD_REGION", "us")
    client = speech.SpeechClient(
        client_options={"api_endpoint": f"{region}-speech.googleapis.com"}
    )
    primary = "hi-IN" if source_lang.startswith("hi") else "en-IN"
    config = speech.RecognitionConfig(
        auto_decoding_config=speech.AutoDetectDecodingConfig(),
        language_codes=[primary],
        model="chirp_3",
        features=speech.RecognitionFeatures(
            enable_word_time_offsets=True,
            diarization_config=speech.SpeakerDiarizationConfig(),
        ),
    )
    with open(wav_path, "rb") as f:
        content = f.read()
    request = speech.RecognizeRequest(
        recognizer=f"projects/{project}/locations/{region}/recognizers/_",
        config=config,
        content=content,
    )
    resp = client.recognize(request=request)
    words: list = []
    votes: dict = {}
    text_parts = []
    try:
        import wave

        with wave.open(wav_path, "rb") as _w:
            audio_s = _w.getnframes() / (_w.getframerate() or 1)
    except Exception:
        audio_s = 0.0
    try:
        from services.dub_worker import costs as _costs

        _costs.record("chirp", audio_s, Path(wav_path).name)
    except Exception:
        pass
    for r in resp.results:
        alt = r.alternatives[0]
        text_parts.append(alt.transcript)
        for w in alt.words:
            spk = str(getattr(w, "speaker_label", "1") or "1")
            votes[spk] = votes.get(spk, 0) + 1
            words.append({"word": w.word, "speaker": spk})
    text = " ".join(text_parts).strip()
    if not text:
        raise ValueError("empty chirp transcript")
    top = max(votes, key=votes.get) if votes else "0"
    # chirp tags are 0-based and unbounded; our voice map is 1..3 — clamp
    n = int(top) + 1 if str(top).isdigit() else 1
    label = str(min(max(n, 1), 3))
    for w in words:
        if str(w["speaker"]).isdigit():
            w["speaker"] = str(min(max(int(w["speaker"]) + 1, 1), 3))
    return {
        "text": text,
        "confidence": 0.95,
        "provider": "chirp",
        "speaker_label": label,
        "words": words,
    }


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

    Translations endpoint is English-only; diarization comes from Chirp
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
    if os.getenv("LINGOFY_USE_CHIRP") and os.getenv("GOOGLE_CLOUD_PROJECT"):
        try:
            return transcribe_chirp(wav_path, source_lang)
        except Exception:
            pass
    try:
        from faster_whisper import WhisperModel

        global _FW_MODEL
        try:
            _FW_MODEL
        except NameError:
            _FW_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
        segs, info = _FW_MODEL.transcribe(wav_path, language=source_lang, beam_size=1)
        text = " ".join(s.text.strip() for s in segs).strip()
        if not text:
            raise ValueError("empty transcript")
        return {"text": text, "confidence": 0.8, "provider": "faster-whisper"}
    except Exception:
        return {"text": f"mock transcript {Path(wav_path).name} {source_lang}", "confidence": 1.0, "provider": "mock"}
