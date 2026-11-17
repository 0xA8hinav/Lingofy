"""File-level speaker diarization via Gemini audio (free tier).

One call per video -> globally consistent speaker IDs, cached per
session. Key rotation across the SVAR key pool on 429/quota errors.
"""
import json
import os
import time
from pathlib import Path

CACHE = Path("data/diarize")
KEYS_FILE = "/home/abhinav/Documents/github/SVAR/credentials/gemini-api-keys.json"


def _keys() -> list:
    try:
        return json.load(open(KEYS_FILE))["keys"]
    except Exception:
        k = os.getenv("GEMINI_API_KEY")
        return [k] if k else []


def _prompt(total_s: float) -> str:
    return (
        "Identify the speakers in this audio. Return ONLY a JSON array like "
        '[{"start":0.0,"end":8.5,"speaker":"SPEAKER_1"}]. Max 3 speakers. '
        f"Cover the full {total_s:.0f} seconds with no gaps."
    )


def diarize_file(audio_path: str, total_s: float) -> list:
    """Return [{start, end, speaker}] with SPEAKER_N normalized to 1..3."""
    from google import genai
    from google.genai import types

    keys = _keys()
    if not keys:
        raise RuntimeError("no gemini keys")
    data = Path(audio_path).read_bytes()
    if len(data) > 18 * 1024 * 1024:
        raise ValueError("audio too large for inline; split first")
    last_err = None
    for i, key in enumerate(keys):
        try:
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    _prompt(total_s),
                    types.Part.from_bytes(data=data, mime_type="audio/wav"),
                ],
            )
            text = (resp.text or "").strip().strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
            segs = json.loads(text)
            out = []
            for s in segs:
                spk = str(s.get("speaker", "SPEAKER_1"))
                n = "".join(ch for ch in spk if ch.isdigit()) or "1"
                out.append({"start": float(s["start"]), "end": float(s["end"]),
                            "speaker": str(min(max(int(n), 1), 3))})
            return sorted(out, key=lambda x: x["start"])
        except Exception as e:
            last_err = e
            time.sleep(2)
            continue
    raise RuntimeError(f"gemini diarization failed on all keys: {last_err}")


def speaker_at(segments: list, t_s: float) -> str:
    for s in segments:
        if s["start"] <= t_s < s["end"]:
            return s["speaker"]
    return segments[-1]["speaker"] if segments else "1"
