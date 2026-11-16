VOICE_MAP = {
    "en": {"1": "en_US-lessac-medium", "2": "en_US-ryan-medium", "3": "en_US-ryan-medium+pitch-shift"},
    "hi": {"1": "hi_IN-pratham-medium", "2": "hi_IN-priyamvada-medium", "3": "hi_IN-pratham-medium+pitch-shift"},
}


def voice_for(speaker_label: str, lang: str) -> str:
    return VOICE_MAP.get(lang, VOICE_MAP["en"]).get(speaker_label, VOICE_MAP["en"]["1"])


def cost_cents(chars: int, provider: str) -> float:
    return 0.0


def translate(text: str, source_lang: str, target_lang: str) -> dict:
    return {"target_text": f"[{target_lang}] {text}", "chars_billed": len(text)}


def synthesize(text: str, target_lang: str, speaker_label: str, out_dir: str = "data/dubbed") -> dict:
    from pathlib import Path

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    mp3 = str(Path(out_dir) / f"{speaker_label}_{abs(hash(text)) % 9999}.mp3")
    Path(mp3).write_bytes(b"ID3" + text.encode()[:100] + b"\x00" * 100)
    return {"mp3_path": mp3, "provider": "piper"}
