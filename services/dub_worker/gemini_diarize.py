"""File-level speaker diarization via Gemini audio (free tier).

One call per video -> globally consistent speaker IDs, cached per
session. Key rotation across the SVAR key pool on 429/quota errors.
"""
import json
import os
import time
from pathlib import Path

CACHE = Path("data/diarize")
TCACHE = Path("data/transcripts")
KEYS_FILE = "/home/abhinav/Documents/github/SVAR/credentials/gemini-api-keys.json"


def _keys() -> list:
    try:
        return json.load(open(KEYS_FILE))["keys"]
    except Exception:
        k = os.getenv("GEMINI_API_KEY")
        return [k] if k else []


_key_cursor = 0


def _ordered_keys() -> list:
    """Round-robin key order; spreads quota usage instead of hammering key[0]."""
    global _key_cursor
    keys = _keys()
    if not keys:
        return []
    start = _key_cursor % len(keys)
    _key_cursor += 1
    return keys[start:] + keys[:start]


def _is_quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return "429" in s or "quota" in s or "exhaust" in s or "rate" in s


def _prompt(total_s: float) -> str:
    return (
        "Identify ALL distinct speakers in this audio, however many there are. "
        "Return ONLY a JSON array like "
        '[{"start":0.0,"end":8.5,"speaker":"SPEAKER_1"}]. '
        f"Cover the full {total_s:.0f} seconds with no gaps."
    )
    return (
        "Identify ALL distinct speakers in this audio, however many there are. "
        "Return ONLY a JSON array like "
        '[{"start":0.0,"end":8.5,"speaker":"SPEAKER_1"}]. '
        f"Cover the full {total_s:.0f} seconds with no gaps."
    )


def diarize_file(audio_path: str, total_s: float) -> list:
    """Native diarization via gemini-3.5-transcribe: up to 8 speakers + word times.

    Falls back to the 2.5-flash JSON prompt if the transcribe API is unavailable.
    """
    try:
        return _diarize_long(audio_path, total_s)
    except Exception as e:
        return _diarize_json_prompt(audio_path, total_s, e)


def _diarize_long(audio_path: str, total_s: float, part_s: float = 180.0) -> list:
    """Split long audio into parts (API timeouts), diarize each, reconcile IDs."""
    import subprocess
    import tempfile
    import wave

    with wave.open(audio_path, "rb") as _w:
        _sr, _n = _w.getframerate(), _w.getnframes()
        _raw = _w.readframes(_n)
    import struct as _st

    _vals = _st.unpack("<" + "h" * (len(_raw) // 2), _raw) if _raw else (0,)
    dur = len(_vals) / max(1, _sr)
    if dur <= part_s + 30:
        return _diarize_transcribe_api(audio_path)
    tmp = Path(tempfile.mkdtemp(prefix="dzpart"))
    all_segs, reps = [], []
    off = 0.0
    idx = 0
    while off < dur:
        a, b = int(off * _sr), int(min(dur, off + part_s) * _sr)
        pp = str(tmp / f"part{idx}.wav")
        with wave.open(pp, "wb") as _o:
            _o.setnchannels(1)
            _o.setsampwidth(2)
            _o.setframerate(_sr)
            _o.writeframes(_st.pack("<" + "h" * max(0, b - a), *_vals[a:b]))
        try:
            psegs = _diarize_transcribe_api(pp)
        except Exception:
            psegs = [{"start": 0.0, "end": b / _sr - a / _sr, "speaker": "1"}]
        for s in psegs:
            s["start"] += off
            s["end"] += off
            all_segs.append(s)
            mid = (s["start"] + s["end"]) / 2
            reps.append((s["speaker"], idx, mid))
        off += part_s
        idx += 1
    # reconcile part-local IDs -> global via ECAPA on segment centers
    try:
        from services.dub_worker import ecapa as _ec
        import numpy as _np

        wavs = []
        for _i, s in enumerate(all_segs):
            _m = int((s["start"] + s["end"]) / 2 * _sr)
            _h = min(2 * _sr, int((s["end"] - s["start"]) / 2 * _sr) or 2 * _sr)
            _cut = _vals[max(0, _m - _h):_m + _h]
            _p = str(tmp / f"seg{_i}.wav")
            with wave.open(_p, "wb") as _o:
                _o.setnchannels(1)
                _o.setsampwidth(2)
                _o.setframerate(_sr)
                _o.writeframes(_st.pack("<" + "h" * len(_cut), *_cut))
            wavs.append(_p)
        _map = _ec.session_map(wavs)
        for s, _p in zip(all_segs, wavs):
            s["speaker"] = _map[_p]
    except Exception:
        pass
    # merge per-part word caches into one full-audio transcript cache
    try:
        import json as _js

        merged = []
        _off = 0.0
        for _i in range(idx):
            _cf = _tkey(str(tmp / f"part{_i}.wav"))
            if _cf.exists():
                for _w in _js.loads(_cf.read_text()):
                    _w["start"] += _off
                    _w["end"] += _off
                    merged.append(_w)
            _off += part_s
        if merged:
            TCACHE.mkdir(parents=True, exist_ok=True)
            _tkey(audio_path).write_text(_js.dumps(merged))
    except Exception:
        pass
    return sorted(all_segs, key=lambda x: x["start"])


def _diarize_transcribe_api(audio_path: str) -> list:
    from google import genai

    keys = _ordered_keys()
    if not keys:
        raise RuntimeError("no gemini keys")
    last_err = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            up = client.files.upload(file=audio_path)
            resp = client.interactions.create(
                model="gemini-3.5-transcribe",
                input=[up],
                generation_config={
                    "transcription_config": {
                        "language_codes": [],
                        "mode": {
                            "type": "verbatim",
                            "diarization_mode": "speaker",
                            "timestamp_granularities": ["word"],
                        },
                    }
                },
            )
            words = []
            for step in getattr(resp, "steps", []) or []:
                for content in getattr(step, "content", []) or []:
                    for a in getattr(content, "annotations", []) or []:
                        if getattr(a, "type", None) == "word_info":
                            words.append(a)
            if not words:
                raise ValueError("no word annotations returned")
            _save_transcript_cache(audio_path, words)
            return _words_to_segments(words)
        except Exception as e:
            last_err = e
            time.sleep(0.5 if _is_quota_error(e) else 2)
            continue
    raise RuntimeError(f"transcribe api failed on all keys: {last_err}")


def _tkey(audio_path: str) -> Path:
    import hashlib

    h = hashlib.sha1()
    with open(audio_path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return TCACHE / f"{h.hexdigest()}.json"


def _save_transcript_cache(audio_path: str, words) -> None:
    try:
        TCACHE.mkdir(parents=True, exist_ok=True)
        out = []
        for w in words:
            s, e = _off(w, "start_offset"), _off(w, "end_offset")
            out.append({"word": getattr(w, "text", ""), "start": s, "end": e,
                        "speaker": str(getattr(w, "speaker", "") or "")})
        _tkey(audio_path).write_text(json.dumps(out))
    except Exception:
        pass


def _off(w, attr: str) -> float:
    v = getattr(w, attr, None)
    try:
        return float(v.total_seconds()) if hasattr(v, "total_seconds") else float(v or 0)
    except Exception:
        return 0.0


def chunk_text(audio_path: str, start_s: float, end_s: float):
    """Slice cached transcript words for [start, end] -> (text, speaker)."""
    p = _tkey(audio_path)
    if not p.exists():
        return "", ""
    words = json.loads(p.read_text())
    use = [w for w in words if w["end"] > start_s and w["start"] < end_s]
    if not use:
        return "", ""
    text = " ".join(w["word"] for w in use).strip()
    votes: dict = {}
    for w in use:
        n = "".join(ch for ch in str(w.get("speaker", "")) if ch.isdigit()) or "1"
        lab = str(min(max(int(n), 1), 6))
        votes[lab] = votes.get(lab, 0) + (w["end"] - w["start"])
    speaker = max(votes, key=votes.get)
    return text, speaker


def _words_to_segments(words: list) -> list:
    """Merge consecutive same-speaker words into segments, cap 8 speakers."""
    order: dict = {}
    segs, cur = [], None
    for w in words:
        raw = str(getattr(w, "speaker", "") or "")
        n = "".join(ch for ch in raw if ch.isdigit()) or "1"
        lab = str(min(max(int(n), 1), 8))
        if lab not in order:
            order[lab] = str(len(order) + 1)
        lab = order[lab]
        start = getattr(w, "start_offset", None)
        end = getattr(w, "end_offset", None)
        try:
            s = float(start.total_seconds()) if hasattr(start, "total_seconds") else float(start or 0)
            e = float(end.total_seconds()) if hasattr(end, "total_seconds") else float(end or 0)
        except Exception:
            s, e = 0.0, 0.0
        if cur and cur["speaker"] == lab:
            cur["end"] = max(cur["end"], e)
        else:
            cur = {"start": s, "end": max(e, s), "speaker": lab}
            segs.append(cur)
    return segs


def _diarize_json_prompt(audio_path: str, total_s: float, _cause: Exception) -> list:
    """Fallback: 2.5-flash JSON segment prompt (auto-detect count, cap 6)."""
    from google import genai
    from google.genai import types

    keys = _ordered_keys()
    if not keys:
        raise RuntimeError("no gemini keys")
    data = Path(audio_path).read_bytes()
    if len(data) > 18 * 1024 * 1024:
        raise ValueError("audio too large for inline; split first")
    last_err = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            from google.genai import types as _t

            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    _prompt(total_s),
                    _t.Part.from_bytes(data=data, mime_type="audio/wav"),
                ],
                config=_t.GenerateContentConfig(max_output_tokens=9000),
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
                            "speaker": str(min(max(int(n), 1), 6))})
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
            time.sleep(0.5 if _is_quota_error(e) else 2)
            continue
    raise RuntimeError(f"gemini diarization failed on all keys: {last_err}")


def speaker_at(segments: list, t_s: float) -> str:
    for s in segments:
        if s["start"] <= t_s < s["end"]:
            return s["speaker"]
    return segments[-1]["speaker"] if segments else "1"
