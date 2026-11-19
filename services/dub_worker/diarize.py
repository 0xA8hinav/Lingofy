"""Pitch-based CPU diarization: F0 median per chunk + window smoothing.

Male/female voices differ most in fundamental frequency (~90-150Hz vs
~170-280Hz). Energy clustering flips mid-sentence; F0 with a majority
window keeps one voice per sentence. ECAPA remains the upgrade path.
"""

import struct
import wave


def _f0_median(vals, sr=16000) -> float:

    n = len(vals)
    if n < sr // 2:
        return 0.0
    # mean-removed autocorrelation over 50..400Hz lags
    mean = sum(vals) / n
    x = [v - mean for v in vals[::4]]  # 4kHz decimation for speed
    sr_d = sr // 4
    lo, hi = max(1, sr_d // 400), min(len(x) // 2, sr_d // 50)
    best_v = 0.0
    curve = []
    e0 = sum(v * v for v in x) or 1.0
    step = max(1, (hi - lo) // 60)
    for lag in range(lo, hi, step):
        s = sum(a * b for a, b in zip(x, x[lag : lag + len(x) - lag]))
        v = s / e0
        curve.append((lag, v))
        best_v = max(best_v, v)
    if best_v < 0.25:
        return 0.0
    # first strong peak (fundamental), not the global max (often a subharmonic
    # an octave below, e.g. 66Hz on a ~200Hz female voice)
    for lag, v in curve:
        if v > best_v * 0.6:
            return sr_d / lag
    return 0.0


def _embed(wav_path: str) -> list[float]:
    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        fmt = "<" + "h" * (len(raw) // 2)
        vals = struct.unpack(fmt, raw) if raw else (0,)
        f0 = _f0_median(list(vals), w.getframerate())
        # 8s chunk may hold 2 turns; split halves and keep the steadier half
        h = len(vals) // 2
        f1 = _f0_median(list(vals[:h]), w.getframerate())
        f2 = _f0_median(list(vals[h:]), w.getframerate())
        dom = f1 if abs(f1 - f0) <= abs(f2 - f0) else f2
        return [float(dom or f0)]


def cluster_session(embeddings: list[list[float]]) -> list[int]:
    # absolute pitch split: <=160Hz -> 1 (low), else 2 (high); unvoiced (0) carried forward
    out, last = [], 1
    for e in embeddings:
        f = e[0]
        if f <= 0:
            out.append(last)
        else:
            last = 1 if f <= 200 else 2
            out.append(last)
    return out


def _smooth(labels: list[int], width: int = 5) -> list[int]:
    out = list(labels)
    for i in range(len(out)):
        win = out[max(0, i - width // 2) : i + width // 2 + 1]
        out[i] = max(set(win), key=win.count)
    # snap isolated singles flanked by the same label
    for i in range(1, len(out) - 1):
        if out[i - 1] == out[i + 1] != out[i]:
            out[i] = out[i - 1]
    return out


def classify_gender(f0_hz: float) -> str:
    """Male ~85-155Hz, female ~185-255Hz. Narrow deadband -> 'unknown' (falls back to label map)."""
    if not f0_hz or f0_hz <= 0:
        return "unknown"
    if f0_hz < 165:
        return "male"
    if f0_hz > 175:
        return "female"
    return "unknown"


_GENDER_CLF = None


def classify_gender_nn(wav_path: str) -> str:
    """Neural gender check: wav2vec2 gender model, 1 sample per speaker.

    Free, local after first download. Returns male/female/unknown.
    """
    global _GENDER_CLF
    try:
        if _GENDER_CLF is None:
            from transformers import pipeline as _pl

            _GENDER_CLF = _pl(
                "audio-classification",
                model="alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech",
                device="cpu",
            )  # keep GPU free for demucs
        out = _GENDER_CLF(wav_path)
        top = max(out, key=lambda x: x["score"])
        if top["score"] < 0.7:
            return "unknown"
        lab = top["label"].lower()
        return "male" if "male" in lab and "female" not in lab else "female" if "female" in lab else "unknown"
    except Exception:
        return "unknown"


def gender_for_wav(wav_path: str) -> str:
    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        vals = list(struct.unpack("<" + "h" * (len(raw) // 2), raw)) if raw else [0]
        return classify_gender(_f0_median(vals, w.getframerate()))


def label_chunk(wav_path: str, session_id: str) -> str:
    e = _embed(wav_path)
    lab = cluster_session([e])[0]
    return str(lab)
    e = _embed(wav_path)
    lab = cluster_session([e])[0]
    return str(lab)


def label_session(wav_paths: list[str]) -> dict[str, str]:
    """Two-pass joint clustering + smoothing: one voice per sentence."""
    embs = [_embed(p) for p in wav_paths]
    labels = _smooth(cluster_session(embs))
    return {p: str(l) for p, l in zip(wav_paths, labels)}


def smooth_labels(session_id: str) -> None:
    return None
