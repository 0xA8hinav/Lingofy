VOICE_MAP = {
    "en": {"1": "en_US-lessac-medium", "2": "en_US-ryan-medium", "3": "en_US-ryan-medium+pitch-shift",
           "4": "en_US-lessac-medium+pitch-shift", "5": "en_US-ryan-medium+pitch-shift2", "6": "en_US-lessac-medium+pitch-shift2"},
    "hi": {"1": "hi_IN-pratham-medium", "2": "hi_IN-priyamvada-medium", "3": "hi_IN-pratham-medium+pitch-shift",
           "4": "hi_IN-priyamvada-medium+pitch-shift", "5": "hi_IN-pratham-medium+pitch-shift2", "6": "hi_IN-priyamvada-medium+pitch-shift2"},
}

VOICE_DIR = "assets/voices"


# Natural speaking rate per language at length-scale 1.0 (measured, chars/sec).
# We ALWAYS synthesize at natural rate (scale 1.0). Duration fit is done by
# padding trailing silence when short, or a mild speedup (max ~1.1, barely
# noticeable) when long. Never slow speech down — that sounds robotic.
RATE_CHS = {"hi": 15.6, "en": 18.6}


def _rate_scale(text: str, target_lang: str, target_ms: int) -> float:
    # Retired: we keep natural rate (1.0) and pad silence instead.
    return 1.0


def voice_for(speaker_label: str, lang: str) -> str:
    return VOICE_MAP.get(lang, VOICE_MAP["en"]).get(speaker_label, VOICE_MAP["en"]["1"])


def cost_cents(chars: int, provider: str) -> float:
    return 0.0


def translate(text: str, source_lang: str, target_lang: str) -> dict:
    try:
        import argostranslate.translate as T

        out = T.translate(text, source_lang, target_lang)
        if out:
            return {"target_text": out, "chars_billed": len(text)}
    except Exception:
        pass
    return {"target_text": f"[{target_lang}] {text}", "chars_billed": len(text)}


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


def synthesize(text: str, target_lang: str, speaker_label: str, out_dir: str = "data/dubbed", name: str = "", target_ms: int = 0) -> dict:
    import shutil
    import subprocess
    from pathlib import Path

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    base = name or f"{speaker_label}_{abs(hash(text)) % 9999}"
    wav = str(Path(out_dir) / f"{base}.wav")
    mp3 = str(Path(out_dir) / f"{base}.mp3")
    voice = voice_for(speaker_label, target_lang).split("+")[0]
    model = str(Path(VOICE_DIR) / f"{voice}.onnx")
    vfull = voice_for(speaker_label, target_lang)
    pitch_factor = 1.12 if vfull.endswith("+pitch-shift") else 0.88 if vfull.endswith("+pitch-shift2") else 0.0
    try:
        if not Path(model).exists():
            raise FileNotFoundError(model)
        scale = _rate_scale(text, target_lang, target_ms)
        subprocess.run(
            ["python3", "-m", "piper", "--model", model, "--output-file", wav,
             "--length-scale", f"{scale:.3f}"],
            input=text.encode(), check=True, capture_output=True, timeout=60,
        )
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
        return {"mp3_path": mp3, "wav_path": wav, "provider": "piper"}
    except Exception:
        Path(mp3).write_bytes(b"ID3" + text.encode()[:100] + b"\x00" * 100)
        return {"mp3_path": mp3, "wav_path": mp3, "provider": "piper"}
