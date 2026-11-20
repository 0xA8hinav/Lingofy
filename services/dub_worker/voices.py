VOICE_MAP = {
    "en": {
        "1": "en_US-lessac-medium",
        "2": "en_US-ryan-medium",
        "3": "en_US-ryan-medium+pitch-shift",
        "4": "en_US-lessac-medium+pitch-shift",
        "5": "en_US-ryan-medium+pitch-shift2",
        "6": "en_US-lessac-medium+pitch-shift2",
    },
    "hi": {
        "1": "hi_IN-pratham-medium",
        "2": "hi_IN-priyamvada-medium",
        "3": "hi_IN-pratham-medium+pitch-shift",
        "4": "hi_IN-priyamvada-medium+pitch-shift",
        "5": "hi_IN-pratham-medium+pitch-shift2",
        "6": "hi_IN-priyamvada-medium+pitch-shift2",
    },
}

VOICE_DIR = "assets/voices"

# Gender-matched Piper voices. First same-gender speaker gets base, second gets shifted.
GENDER_VOICES = {
    "male": {
        "en": ["en_US-ryan-medium", "en_US-ryan-medium+pitch-shift"],
        "hi": ["hi_IN-pratham-medium", "hi_IN-pratham-medium+pitch-shift"],
    },
    "female": {
        "en": ["en_US-lessac-medium", "en_US-lessac-medium+pitch-shift"],
        "hi": ["hi_IN-priyamvada-medium", "hi_IN-priyamvada-medium+pitch-shift2"],
    },
}

# Sarvam Bulbul v4-flash Hindi + English personas, gender-matched.
# Deterministic per (speaker, gender, index) so voices stay stable across chunks.
SARVAM_VOICES = {
    "male": {
        "hi": ["kabir_hi_conversational", "amit_hi_conversational", "manan_hi_conversational", "sumit_hi_conversational"],
        "en": ["shubh_en_narration", "rohan_en_recovery", "varun_en_ads", "dev_en_conversational"],
    },
    "female": {
        "hi": ["kavya_hi_conversational", "ritu_hi_conversation", "roopa_hi_conversational", "sanchita_hi_assistant"],
        "en": ["simran_en_narration", "neha_en_customer", "ritu_hi_conversation", "roopa_hi_conversational"],
    },
}
SARVAM_KEY_FILE = "~/.config/lingofy/sarvam.json"


def sarvam_voice_for(speaker_label: str, lang: str, gender: str = "", gindex: int = 0) -> str:
    pool = SARVAM_VOICES.get(gender, {}).get(lang) or []
    if pool:
        return pool[gindex % len(pool)]
    # Neutral fallback: conversational Hindi voices read either gender acceptably.
    neutral = {"hi": "kabir_hi_conversational", "en": "shubh_en_narration"}
    base = [neutral.get(lang, neutral["en"])]
    pool = SARVAM_VOICES["male" if str(speaker_label) in ("1", "3", "5") else "female"][lang]
    return pool[gindex % len(pool)] if pool else base[0]


_SARVAM_KEYS: list | None = None
_SARVAM_CURSOR = [0]


def sarvam_keys() -> list:
    """All configured Sarvam keys (primary + backup). Never logged."""
    import json as _js
    import os

    global _SARVAM_KEYS
    if _SARVAM_KEYS is None:
        _SARVAM_KEYS = []
        try:
            with open(os.path.expanduser(SARVAM_KEY_FILE)) as _kf:
                data = _js.load(_kf)
            if isinstance(data.get("keys"), list):
                _SARVAM_KEYS.extend(k for k in data["keys"] if k)
            if data.get("api_key"):
                _SARVAM_KEYS.append(data["api_key"])
            seen, uniq = set(), []
            for k in _SARVAM_KEYS:
                if k not in seen:
                    seen.add(k)
                    uniq.append(k)
            _SARVAM_KEYS = uniq
        except Exception:
            _SARVAM_KEYS = []
    return _SARVAM_KEYS


def sarvam_key() -> str | None:
    keys = sarvam_keys()
    if not keys:
        return None
    k = keys[_SARVAM_CURSOR[0] % len(keys)]
    _SARVAM_CURSOR[0] += 1
    return k


def _offline() -> bool:
    """Offline test mode: never touch paid APIs (Sarvam/Gemini), even if keys
    happen to be present in the environment. Set LINGOFY_OFFLINE=1 in CI."""
    import os

    return os.getenv("LINGOFY_OFFLINE") == "1"


def sarvam_tts(text: str, lang: str, voice: str, out_wav: str, pace: float = 1.0) -> dict:
    """Sarvam Bulbul v4-flash TTS -> 22050 Hz mono wav. Raises on failure (caller falls back).

    Tries each configured key in turn so quota/rate-limit on one key fails
    over to the next instead of killing the chunk.
    """
    import base64
    import json as _js
    import urllib.error
    import urllib.request

    if _offline():
        raise RuntimeError("offline test mode: sarvam disabled")
    keys = sarvam_keys()
    if not keys:
        raise RuntimeError("no sarvam key")
    lang_code = "hi-IN" if str(lang).lower().startswith("hi") else "en-IN"
    body = _js.dumps({
        "text": text,
        "target_language_code": lang_code,
        "speaker": voice,
        "model": "bulbul:v4-flash",
        "pace": min(2.0, max(0.5, pace)),
        "speech_sample_rate": 22050,
        "output_audio_codec": "wav",
        # Low temperature: same persona must sound like the SAME person
        # across chunks. Default 0.6 wanders in energy/intonation per call.
        "temperature": 0.2,
    }).encode()
    last = None
    for key in keys:
        try:
            req = urllib.request.Request(
                "https://api.sarvam.ai/text-to-speech",
                data=body,
                headers={"Content-Type": "application/json", "api-subscription-key": key},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = _js.loads(r.read().decode())
            audios = resp.get("audios") or []
            if not audios:
                raise ValueError(f"sarvam returned no audio: {str(resp)[:200]}")
            raw = base64.b64decode(audios[0])
            with open(out_wav, "wb") as f:
                f.write(raw)
            return {"chars": len(text), "request_id": resp.get("request_id")}
        except urllib.error.HTTPError as e:
            last = e
            try:
                detail = e.read().decode()[:200]
            except Exception:
                detail = ""
            if e.code in (401, 403, 404):
                continue  # bad/revoked key: never retry same key
            if e.code == 429:
                continue  # quota/rate: try next key
            raise RuntimeError(f"sarvam HTTP {e.code}: {detail}")
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"sarvam tts failed on all keys: {last}")

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
    if not _offline() and (source_lang, target_lang) in (("en", "hi"), ("hi", "en")):
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


_GT_CLIENT = None


def _gemini_client():
    """One shared client (rotated on failure) instead of a new one per chunk."""
    global _GT_CLIENT
    if _GT_CLIENT is None:
        from services.dub_worker.gemini_diarize import _keys as _pool

        keys = _pool()
        _GT_CLIENT = [None, keys, [0]]
    return _GT_CLIENT


# House tone: informal like friends talking, but always respectful.
# (Respectful forms, never rude/crude; no Shuddh/bookish words.)
TONE_EN = "simple everyday spoken English, plain words, like a friend talking, polite and respectful in tone"
TONE_HI = (
    "simple everyday spoken Hindi in DEVANAGARI script only (no Roman letters), "
    "like friends talking — informal but respectful (respectful forms, never rude or crude). "
    "Avoid heavy Shuddh Hindi or bookish words"
)


def _translate_one(client, text: str, source_lang: str, target_lang: str) -> str:
    if source_lang == "hi":
        task = (
            f"Translate this Hindi into {TONE_EN}. "
            "Keep roughly the SAME NUMBER OF WORDS as the original. "
            "Output ONLY the English translation, nothing else:\n\n" + text
        )
    else:
        task = (
            f"Translate this English into {TONE_HI}. "
            "Keep roughly the SAME NUMBER OF WORDS as the original English. "
            "Output ONLY the Hindi translation, nothing else:\n\n" + text
        )
    r = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=task,
        config={"max_output_tokens": 9000},
    )
    out = (r.text or "").strip().strip("`").strip()
    if out.lower().startswith("hindi"):
        out = out.split("\n", 1)[-1].strip()
    if not out:
        raise ValueError("empty translation")
    return out


def _translate_gemini(text: str, source_lang: str, target_lang: str) -> str:
    """Colloquial MT via Gemini 3.5 Flash Lite (free tier, rotated keys).

    Keeps word count close to the original so dubbed audio fits the slot.
    """
    from services.dub_worker.gemini_diarize import genai_client as _gc

    _c, keys, cursor = _gemini_client()
    last = None
    for _ in range(len(keys)):
        cursor[0] = (cursor[0] + 1) % len(keys)
        try:
            client = _c or _gc(keys[cursor[0]])
            _c = client
            return _translate_one(client, text, source_lang, target_lang)
        except Exception as e:
            last = e
            _c = None
            continue
    raise RuntimeError(f"gemini translate failed: {last}")


def translate_batch(texts: list, source_lang: str, target_lang: str) -> list:
    """Translate ALL chunk texts in ONE Gemini call (numbered list in/out).

    Returns a list aligned with the input; blanks where the model skipped.
    Offline test mode returns blanks (callers fall back to passthrough).
    """
    if _offline():
        return [""] * len(texts)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    if source_lang == "hi":
        task = (
            f"Translate each numbered line into {TONE_EN}. "
            "Keep roughly the SAME NUMBER OF WORDS as the original. "
            "Reply with the same numbers, one translation per line, nothing else:\n\n" + numbered
        )
    else:
        task = (
            f"Translate each numbered line into {TONE_HI}. "
            "Keep roughly the SAME NUMBER OF WORDS as the original. "
            "Reply with the same numbers, one translation per line, nothing else:\n\n" + numbered
        )
    from services.dub_worker.gemini_diarize import genai_client as _gc2

    _c, keys, cursor = _gemini_client()
    for _ in range(len(keys)):
        cursor[0] = (cursor[0] + 1) % len(keys)
        try:
            client = _c or _gc2(keys[cursor[0]])
            _c = client
            r = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=task,
                config={"max_output_tokens": 9000},
            )
            out = (r.text or "").strip().strip("`")
            got = {}
            for ln in out.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                head, _, rest = ln.partition(".")
                if head.strip().isdigit():
                    got[int(head.strip()) - 1] = rest.strip()
            if len(got) >= max(1, len(texts) // 2):
                return [got.get(i, "") for i in range(len(texts))]
        except Exception:
            _c = None
            continue
    return [""] * len(texts)


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
    if got == target_ms:
        return
    if got < target_ms:
        # pad trailing silence to the EXACT slot: no gaps, no drift
        tmp = str(Path(wav).with_suffix(".pad.wav"))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                wav,
                "-af",
                f"apad=whole_dur={target_ms / 1000:.3f}",
                "-t",
                f"{target_ms / 1000:.3f}",
                tmp,
            ],
            check=True,
            capture_output=True,
        )
        Path(tmp).replace(wav)
        return
    tempo = min(got / target_ms, 1.1)
    tmp = str(Path(wav).with_suffix(".fit.wav"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav, "-filter:a", f"atempo={tempo:.3f}", tmp],
        check=True,
        capture_output=True,
    )
    # exact length guaranteed: trim any atempo rounding residue
    tmp2 = str(Path(wav).with_suffix(".exact.wav"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", tmp, "-t", f"{target_ms / 1000:.3f}", tmp2],
        check=True,
        capture_output=True,
    )
    Path(tmp2).replace(wav)


def synthesize(
    text: str,
    target_lang: str,
    speaker_label: str,
    out_dir: str = "data/dubbed",
    name: str = "",
    target_ms: int = 0,
    gender: str = "",
    gindex: int = 0,
) -> dict:
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

    provider_used = "piper"
    sarvam_voice = sarvam_voice_for(speaker_label, target_lang, gender, gindex)
    provider_used = "piper"
    sarvam_err = None
    # No keys configured (CI/offline): fail fast, no pointless backoff.
    attempts = 3 if sarvam_keys() else 1
    for attempt in range(attempts):
        try:
            info = sarvam_tts(text, target_lang, sarvam_voice, wav)
            provider_used = "sarvam"
            try:
                from services.dub_worker import costs as _costs

                _costs.record("sarvam", 0, f"{info.get('chars', 0)} chars")
            except Exception:
                pass
            break
        except Exception as e:
            sarvam_err = e
            if attempt < 2:
                import time as _time

                _time.sleep(5 * (attempt + 1))  # transient 429/5xx: back off, same voice
    if provider_used != "sarvam":
        # VISIBLE fallback: a Piper chunk among Sarvam chunks sounds like a
        # different person. Log it so the server log shows exactly which chunk.
        print(f"[tts] {base}: sarvam failed ({str(sarvam_err)[:120]}), piper fallback", flush=True)
    if provider_used == "piper" and not Path(wav).exists():
        if not Path(model).exists():
            raise FileNotFoundError(model)
        subprocess.run(
            ["python3", "-m", "piper", "--model", model, "--output-file", wav, "--length-scale", "1.000"],
            input=text.encode(),
            check=True,
            capture_output=True,
            timeout=60,
        )
    try:
        # Pitch variants exist only because Piper has few voices. Sarvam
        # already gets distinct personas per speaker, so leave those untouched.
        if provider_used == "piper" and pitch_factor and shutil.which("ffmpeg"):
            shifted = str(Path(out_dir) / f"{base}_shift.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav, "-filter:a", f"asetrate=22050*{pitch_factor},aresample=22050", shifted],
                check=True,
                capture_output=True,
            )
            Path(shifted).replace(wav)
        _fit_to_target(wav, target_ms)
        # NOTE: no per-chunk mp3 encode — the mux chain stays wav end to end
        # (mp3 gapless padding was adding boundary clicks). mp3 path kept
        # for API compat only.
        return {"mp3_path": mp3, "wav_path": wav, "provider": provider_used}
    except Exception:
        Path(mp3).write_bytes(b"ID3" + text.encode()[:100] + b"\x00" * 100)
        return {"mp3_path": mp3, "wav_path": mp3, "provider": "piper"}
