VOICE_MAP = {
    "en": {"1": "en_US-lessac-medium", "2": "en_US-ryan-medium", "3": "en_US-ryan-medium+pitch-shift",
           "4": "en_US-lessac-medium+pitch-shift", "5": "en_US-ryan-medium+pitch-shift2", "6": "en_US-lessac-medium+pitch-shift2"},
    "hi": {"1": "hi_IN-pratham-medium", "2": "hi_IN-priyamvada-medium", "3": "hi_IN-pratham-medium+pitch-shift",
           "4": "hi_IN-priyamvada-medium+pitch-shift", "5": "hi_IN-pratham-medium+pitch-shift2", "6": "hi_IN-priyamvada-medium+pitch-shift2"},
}

VOICE_DIR = "assets/voices"

# Gender-matched Piper voices. First same-gender speaker gets base, second gets shifted.
GENDER_VOICES = {
    "male": {"en": ["en_US-ryan-medium", "en_US-ryan-medium+pitch-shift"],
             "hi": ["hi_IN-pratham-medium", "hi_IN-pratham-medium+pitch-shift"]},
    "female": {"en": ["en_US-lessac-medium", "en_US-lessac-medium+pitch-shift"],
               "hi": ["hi_IN-priyamvada-medium", "hi_IN-priyamvada-medium+pitch-shift2"]},
}

# Gendered Gemini pools (stable pools; assignment still random within gender).
GEMINI_MALE = ["Puck", "Charon", "Fenrir", "Orus", "Zephyr"]
GEMINI_FEMALE = ["Kore", "Aoede", "Leda", "Callirrhoe", "Sulafat"]

# Gemini prebuilt voices pool; each session speaker gets a random one (stable
# for the session). Hindi-capable neural voices, free tier.
GEMINI_VOICE_POOL = ["Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda",
                     "Orus", "Zephyr", "Sulafat", "Callirrhoe"]
_SESSION_VOICES: dict = {}


def gemini_voice_for(speaker_label: str, gender: str = "") -> str:
    import random

    key = (speaker_label, gender or "any")
    if key not in _SESSION_VOICES:
        if gender == "male":
            pool = [v for v in GEMINI_MALE if v not in _SESSION_VOICES.values()] or GEMINI_MALE
        elif gender == "female":
            pool = [v for v in GEMINI_FEMALE if v not in _SESSION_VOICES.values()] or GEMINI_FEMALE
        else:
            pool = [v for v in GEMINI_VOICE_POOL if v not in _SESSION_VOICES.values()] or GEMINI_VOICE_POOL
        _SESSION_VOICES[key] = random.choice(pool)
    return _SESSION_VOICES[key]


def gemini_tts(text: str, voice: str, out_wav: str) -> None:
    """Gemini neural TTS -> 24kHz wav. Raises on failure (caller falls back)."""
    import json as _json
    import wave as _w

    from google import genai

    keys = _json.load(open("/home/abhinav/Documents/github/SVAR/credentials/gemini-api-keys.json"))["keys"]
    from google.genai import types as _t

    last = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=text,
                config=_t.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=_t.SpeechConfig(voice_config=_t.VoiceConfig(
                        prebuilt_voice_config=_t.PrebuiltVoiceConfig(voice_name=voice))),
                ),
            )
            for p in resp.candidates[0].content.parts:
                inline = getattr(p, "inline_data", None)
                if inline and inline.data:
                    with _w.open(out_wav, "wb") as _o:
                        _o.setnchannels(1)
                        _o.setsampwidth(2)
                        _o.setframerate(24000)
                        _o.writeframes(inline.data)
                    return
            raise ValueError("no audio in gemini tts response")
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"gemini tts failed: {last}")


# Natural speaking rate per language at length-scale 1.0 (measured, chars/sec).
# We ALWAYS synthesize at natural rate (scale 1.0). Duration fit is done by
# padding trailing silence when short, or a mild speedup (max ~1.1, barely
# noticeable) when long. Never slow speech down — that sounds robotic.
RATE_CHS = {"hi": 15.6, "en": 18.6}


def _rate_scale(text: str, target_lang: str, target_ms: int) -> float:
    # Retired: we keep natural rate (1.0) and pad silence instead.
    return 1.0


def voice_for(speaker_label: str, lang: str, gender: str = "", gindex: int = 0) -> str:
    if gender in GENDER_VOICES and lang in GENDER_VOICES[gender]:
        pool = GENDER_VOICES[gender][lang]
        return pool[gindex % len(pool)]
    return VOICE_MAP.get(lang, VOICE_MAP["en"]).get(speaker_label, VOICE_MAP["en"]["1"])


def cost_cents(chars: int, provider: str) -> float:
    return 0.0


def translate(text: str, source_lang: str, target_lang: str) -> dict:
    if (source_lang, target_lang) in (("en", "hi"), ("hi", "en")):
        try:
            out = _translate_gemini(text, source_lang, target_lang)
            if out:
                return {"target_text": out, "chars_billed": 0}
        except Exception:
            pass
    try:
        import argostranslate.translate as T

        out = T.translate(text, source_lang, target_lang)
        if out:
            return {"target_text": out, "chars_billed": len(text)}
    except Exception:
        pass
    return {"target_text": f"[{target_lang}] {text}", "chars_billed": len(text)}


_GT_CURSOR = 0


def _translate_gemini(text: str, source_lang: str, target_lang: str) -> str:
    """Colloquial MT via Gemini 3.5 Flash Lite (free tier, rotated keys).

    Keeps word count close to the original so dubbed audio fits the slot.
    """
    global _GT_CURSOR
    import json as _js

    from google import genai

    keys = _js.load(open("/home/abhinav/Documents/github/SVAR/credentials/gemini-api-keys.json"))["keys"]
    if source_lang == "hi":
        task = ("Translate this Hindi into simple everyday spoken English, plain words. "
                "Keep roughly the SAME NUMBER OF WORDS as the original. "
                "Output ONLY the English translation, nothing else:\n\n" + text)
    else:
        task = ("Translate this English into simple everyday spoken Hindi in DEVANAGARI script only "
                "(no Roman letters). Use bolt-chaal Hindi like friends talking. "
                "Avoid heavy Shuddh Hindi or bookish words. "
                "Keep roughly the SAME NUMBER OF WORDS as the original English. "
                "Output ONLY the Hindi translation, nothing else:\n\n" + text)
    last = None
    for i in range(len(keys)):
        _GT_CURSOR = (_GT_CURSOR + 1) % len(keys)
        try:
            client = genai.Client(api_key=keys[_GT_CURSOR])
            r = client.models.generate_content(model="gemini-3.5-flash-lite", contents=task)
            out = (r.text or "").strip().strip("`").strip()
            if out.lower().startswith("hindi"):
                out = out.split("\n", 1)[-1].strip()
            if out:
                return out
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"gemini translate failed: {last}")


def _wav_ms(path: str) -> int:
    import wave as _w

    with _w.open(path, "rb") as w:
        return int(w.getnframes() / (w.getframerate() or 1) * 1000)


def _fit_to_target(wav: str, target_ms: int) -> None:
    """Match slot duration WITHOUT touching speech rate: pad silence if short,
    mild speedup (max ~1.1x, imperceptible) if slightly long."""
    import shutil
    import subprocess
    from pathlib import Path

    if not target_ms or target_ms <= 0 or not shutil.which("ffmpeg"):
        return
    got = _wav_ms(wav)
    if got <= 0:
        return
    if abs(got - target_ms) <= max(400, target_ms * 0.05):
        return
    if got < target_ms:
        pad_s = (target_ms - got) / 1000
        tmp = str(Path(wav).with_suffix(".pad.wav"))
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav, "-af", f"apad=whole_dur={target_ms / 1000:.3f}",
             "-t", f"{target_ms / 1000:.3f}", tmp],
            check=True, capture_output=True,
        )
        Path(tmp).replace(wav)
        return
    tempo = min(got / target_ms, 1.1)
    tmp = str(Path(wav).with_suffix(".fit.wav"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav, "-filter:a", f"atempo={tempo:.3f}", tmp],
        check=True, capture_output=True,
    )
    Path(tmp).replace(wav)


def synthesize(text: str, target_lang: str, speaker_label: str, out_dir: str = "data/dubbed", name: str = "", target_ms: int = 0, gender: str = "", gindex: int = 0) -> dict:
    import shutil
    import subprocess
    from pathlib import Path

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    base = name or f"{speaker_label}_{abs(hash(text)) % 9999}"
    wav = str(Path(out_dir) / f"{base}.wav")
    mp3 = str(Path(out_dir) / f"{base}.mp3")
    voice = voice_for(speaker_label, target_lang, gender, gindex).split("+")[0]
    model = str(Path(VOICE_DIR) / f"{voice}.onnx")
    vfull = voice_for(speaker_label, target_lang, gender, gindex)
    pitch_factor = 1.12 if vfull.endswith("+pitch-shift") else 0.88 if vfull.endswith("+pitch-shift2") else 0.0
    import os as _os

    provider_used = "piper"
    if _os.getenv("LINGOFY_USE_GEMINI_TTS"):
        try:
            gemini_tts(text, gemini_voice_for(speaker_label, gender), wav)
            provider_used = "gemini-tts"
            if shutil.which("ffmpeg"):
                uni = str(Path(out_dir) / f"{base}_22k.wav")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", wav, "-ar", "22050", "-ac", "1", uni],
                    check=True, capture_output=True,
                )
                Path(uni).replace(wav)
        except Exception:
            provider_used = "piper"
    if provider_used == "piper" and not Path(wav).exists():
        if not Path(model).exists():
            raise FileNotFoundError(model)
        subprocess.run(
            ["python3", "-m", "piper", "--model", model, "--output-file", wav,
             "--length-scale", "1.000"],
            input=text.encode(), check=True, capture_output=True, timeout=60,
        )
    try:
        if pitch_factor and shutil.which("ffmpeg"):
            shifted = str(Path(out_dir) / f"{base}_shift.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav, "-filter:a", f"asetrate=22050*{pitch_factor},aresample=22050", shifted],
                check=True, capture_output=True,
            )
            Path(shifted).replace(wav)
        _fit_to_target(wav, target_ms)
        if shutil.which("ffmpeg"):
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav, "-codec:a", "libmp3lame", "-q:a", "4", mp3],
                check=True, capture_output=True,
            )
        else:
            mp3 = wav
        return {"mp3_path": mp3, "wav_path": wav, "provider": provider_used}
    except Exception:
        Path(mp3).write_bytes(b"ID3" + text.encode()[:100] + b"\x00" * 100)
        return {"mp3_path": mp3, "wav_path": mp3, "provider": "piper"}
