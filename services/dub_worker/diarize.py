"""Lightweight CPU diarization: wav stats + capped clustering (ECAPA swap-in later)."""
import hashlib
import wave
from pathlib import Path

import struct


def _embed(wav_path: str) -> list[float]:
    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        fmt = "<" + "h" * (len(raw) // 2)
        vals = struct.unpack(fmt, raw) if raw else (0,)
        mean = sum(v * v for v in vals) / max(1, len(vals))
        var = sum(((v * v) - mean) ** 2 for v in vals) / max(1, len(vals))
        zero = sum(1 for a, b in zip(vals, vals[1:]) if (a < 0) != (b < 0)) / max(1, len(vals))
        return [float(mean / 1e8), float(var / 1e16), float(zero)]


def cluster_session(embeddings: list[list[float]]) -> list[int]:
    # deterministic capped clustering: sort by first dim, split into <=3 groups
    idx = sorted(range(len(embeddings)), key=lambda i: embeddings[i][0])
    labels = [0] * len(embeddings)
    k = min(3, len(embeddings))
    for rank, i in enumerate(idx):
        labels[i] = min(3, rank * k // max(1, len(embeddings)) + 1)
        if labels[i] < 1:
            labels[i] = 1
    return labels


def label_chunk(wav_path: str, session_id: str) -> str:
    e = _embed(wav_path)
    lab = cluster_session([e])[0]
    return str(lab)


def label_session(wav_paths: list[str]) -> dict[str, str]:
    """Two-pass joint clustering: steady labels across a session (OSS, CPU)."""
    embs = [_embed(p) for p in wav_paths]
    labels = cluster_session(embs)
    return {p: str(l) for p, l in zip(wav_paths, labels)}


def smooth_labels(session_id: str) -> None:
    return None
