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
        mean = sum(vals) / max(1, len(vals))
        var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals))
        h = int(hashlib.md5(Path(wav_path).name.encode()).hexdigest()[:8], 16) % 1000 / 1000
        return [float(mean / 1000), float(var / 1e6), float(h)]


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


def smooth_labels(session_id: str) -> None:
    return None
