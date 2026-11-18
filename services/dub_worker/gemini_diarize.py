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

        wavs = []
        for _i, s in enumerate(all_segs):
            _m = int((s["start"] + s["end"]) / 2 * _sr)
            _h = min(2 * _sr, int((s["end"] - s["start"]) / 2 * _sr) or 2 * _sr)
            _cut = _vals[max(0, _m - _h) : _m + _h]
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
    from google.genai import interactions as I

    keys = _ordered_keys()
    if not keys:
        raise RuntimeError("no gemini keys")
    last_err = None
    for key in keys:
        try:
            from google import genai

            client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
            up = client.files.upload(file=audio_path)
            uri = getattr(up, "uri", None) or getattr(up, "name", "")
            mime = getattr(up, "mime_type", None) or "audio/wav"
            resp = client.interactions.create(
                model="gemini-3.5-transcribe",
                input=[I.UserInputStep(content=[I.AudioContent(uri=uri, mime_type=mime)])],
                generation_config=I.GenerationConfig(
                    transcription_config=I.TranscriptionConfig(
                        language_codes=[],
                        mode=I.VerbatimTranscriptionMode(
                            diarization_mode="speaker",
                            timestamp_granularities=["word"],
                        ),
                    )
                ),
            )
            words = []
            for step in getattr(resp, "steps", []) or []:
                for content in getattr(step, "content", []) or []:
                    for a in getattr(content, "annotations", []) or []:
                        if getattr(a, "type", None) == "word_info":
                            words.append(a)
            if not words:
                words = _extract_words(resp)
            if not words:
                raise ValueError("no word annotations returned")
            _save_transcript_cache(audio_path, words)
            return _words_to_segments(words)
        except Exception as e:
            last_err = e
            time.sleep(0.5 if _is_quota_error(e) else 2)
            continue
    raise RuntimeError(f"transcribe api failed on all keys: {last_err}")


def _extract_words(resp) -> list:
    """Fallback word extraction across response shapes (preview API churn)."""

    found = []
    seen = set()

    def _walk(o):
        oid = id(o)
        if oid in seen or len(found) > 20000:
            return
        seen.add(oid)
        if isinstance(o, dict):
            t = o.get("type") or o.get("@type") or ""
            if t == "word_info" or ("word" in o and ("start_offset" in o or "startOffset" in o or "start" in o)):
                found.append(o)
                return
            for v in o.values():
                _walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                _walk(v)
        else:
            for attr in ("steps", "content", "annotations", "parts"):
                try:
                    v = getattr(o, attr, None)
                except Exception:
                    v = None
                if v is not None:
                    _walk(v)
                    break
            else:
                try:
                    d = o.model_dump()
                except Exception:
                    return
                _walk(d)

    try:
        _walk(resp)
    except Exception:
        pass
    out = []
    for w in found:
        if isinstance(w, dict):
            out.append(_dict_word(w))
    return out


def _dict_word(w: dict):
    class _W:
        pass

    o = _W()
    o.word = w.get("word") or w.get("text", "")
    o.start_offset = w.get("start_offset", w.get("startOffset", w.get("start", 0)))
    o.end_offset = w.get("end_offset", w.get("endOffset", w.get("end", 0)))
    spk = w.get("speaker", "")
    o.speaker = spk.get("id", spk) if isinstance(spk, dict) else spk
    o.type = "word_info"
    return o


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
            if isinstance(w, dict):
                wd = w.get("word") or w.get("text", "")
                spk = w.get("speaker", "")
            else:
                wd = getattr(w, "text", "")
                spk = getattr(w, "speaker", "")
            s, e = _off(w, "start_offset"), _off(w, "end_offset")
            out.append({"word": wd, "start": s, "end": e, "speaker": str(spk or "")})
        _tkey(audio_path).write_text(json.dumps(out))
    except Exception:
        pass


def _off(w, attr: str) -> float:
    v = w.get(attr) if isinstance(w, dict) else getattr(w, attr, None)
    try:
        if hasattr(v, "total_seconds"):
            return float(v.total_seconds())
        if isinstance(v, str):
            v = v.strip()
            v = v.removesuffix("s")
            return float(v)
        return float(v or 0)
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
        raw = str(w.get("speaker", ""))
        digs = "".join(ch for ch in raw if ch.isdigit())
        n = int(digs) if digs else 1
        if raw.startswith("spk:"):
            n += 1
        lab = str(min(max(n, 1), 6))
        votes[lab] = votes.get(lab, 0) + (w["end"] - w["start"])
    speaker = max(votes, key=votes.get)
    return text, speaker


def _words_to_segments(words: list) -> list:
    """Merge consecutive same-speaker words into segments, cap 8 speakers."""
    order: dict = {}
    segs, cur = [], None
    for w in words:
        raw = str(getattr(w, "speaker", "") or "")
        digs = "".join(ch for ch in raw if ch.isdigit())
        n = int(digs) if digs else 1
        if raw.startswith("spk:"):
            n += 1  # spk:0-based -> 1-based
        lab = str(min(max(n, 1), 8))
        if lab not in order:
            order[lab] = str(len(order) + 1)
        lab = order[lab]
        start = _off(w, "start_offset")
        end = _off(w, "end_offset")
        try:
            s, e = float(start), float(end)
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

    keys = _ordered_keys()
    if not keys:
        raise RuntimeError("no gemini keys")
    data = Path(audio_path).read_bytes()
    if len(data) > 18 * 1024 * 1024:
        raise ValueError("audio too large for inline; split first")
    last_err = None
    for key in keys:
        try:
            client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
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
                out.append({"start": float(s["start"]), "end": float(s["end"]), "speaker": str(min(max(int(n), 1), 6))})
            segs = sorted(out, key=lambda x: x["start"])
            # 3.5-lite finds boundaries but often returns one label: identity via local ECAPA
            if len(segs) > 1 and len({s["speaker"] for s in segs}) == 1:
                try:
                    import tempfile
                    import wave

                    from services.dub_worker import ecapa as _ec

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
                        _cut = _vals[_mid - _half : _mid + _half] or _vals[_a:_b]
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
