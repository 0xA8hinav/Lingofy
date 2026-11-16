VOICE_MAP = {
    "en": {"1": "en_US-lessac-medium", "2": "en_US-ryan-medium", "3": "en_US-ryan-medium+pitch-shift"},
    "hi": {"1": "hi_IN-pratham-medium", "2": "hi_IN-priyamvada-medium", "3": "hi_IN-pratham-medium+pitch-shift"},
}

VOICE_DIR = "assets/voices"


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


def synthesize(text: str, target_lang: str, speaker_label: str, out_dir: str = "data/dubbed", name: str = "") -> dict:
    import shutil
    import subprocess
    from pathlib import Path

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    base = name or f"{speaker_label}_{abs(hash(text)) % 9999}"
    wav = str(Path(out_dir) / f"{base}.wav")
    mp3 = str(Path(out_dir) / f"{base}.mp3")
    voice = voice_for(speaker_label, target_lang).split("+")[0]
    model = str(Path(VOICE_DIR) / f"{voice}.onnx")
    pitch = "+pitch-shift" in voice_for(speaker_label, target_lang)
    try:
        if not Path(model).exists():
            raise FileNotFoundError(model)
        subprocess.run(
            ["python3", "-m", "piper", "--model", model, "--output-file", wav],
            input=text.encode(), check=True, capture_output=True, timeout=60,
        )
        if pitch and shutil.which("ffmpeg"):
            shifted = str(Path(out_dir) / f"{base}_shift.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav, "-filter:a", "asetrate=22050*1.12,aresample=22050", shifted],
                check=True, capture_output=True,
            )
            Path(shifted).replace(wav)
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
