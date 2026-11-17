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
            segs = sorted(out, key=lambda x: x["start"])
            # 3.5-lite finds boundaries but often returns one label: identity via local ECAPA
            if len(segs) > 1 and len({s["speaker"] for s in segs}) == 1:
                try:
                    from services.dub_worker import ecapa as _ec
                    import tempfile
                    import wave

                    with wave.open(audio_path, "rb") as _w:
                        _sr, _n = _w.getframerate(), _w.getnframes()
                        _raw = _w.readframes(_n)
                    import struct as _st

                    _vals = _st.unpack("<" + "h" * (len(_raw) // 2), _raw) if _raw else (0,)
                    _tmp = Path(tempfile.mkdtemp(prefix="segspk"))
                    _paths = []
                    for _i, _s in enumerate(segs):
                        _a = max(0, int(_s["start"] * _sr))
                        _b = min(len(_vals), int(_s["end"] * _sr))
                        _mid = (_a + _b) // 2
                        _half = min(2 * _sr, (_b - _a) // 2)
                        _cut = _vals[_mid - _half:_mid + _half] or _vals[_a:_b]
                        _p = str(_tmp / f"seg{_i}.wav")
                        with wave.open(_p, "wb") as _o:
                            _o.setnchannels(1)
                            _o.setsampwidth(2)
                            _o.setframerate(_sr)
                            _o.writeframes(_st.pack("<" + "h" * len(_cut), *_cut))
                        _paths.append(_p)
                    _map = _ec.session_map(_paths)
                    for _s, _p in zip(segs, _paths):
                        _s["speaker"] = _map[_p]
                except Exception:
                    pass
            return segs
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
