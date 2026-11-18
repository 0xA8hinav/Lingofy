"""Session-level speaker profiles: SpeechBrain ECAPA embeddings + global clustering.

One pass over all chunks at upload gives file-consistent speaker IDs
(unlike per-chunk votes). Temporal smoothing kills single-chunk flips.
"""

import struct
import wave

import numpy as np

_classifier = None


def _get_classifier():
    global _classifier
    if _classifier is not None:
        return _classifier
    from speechbrain.inference.speaker import EncoderClassifier

    # CPU-only: the box has no usable GPU; "cuda" string breaks device parsing
    _classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": "cpu"},
    )
    return _classifier


def embed_wav(path: str) -> np.ndarray:
    import torch

    with wave.open(path, "rb") as w:
        n, sr = w.getnframes(), w.getframerate()
        raw = w.readframes(n)
        vals = struct.unpack("<" + "h" * (len(raw) // 2), raw) if raw else (0,)
    arr = np.array(vals, dtype=np.float32) / 32768.0
    if sr != 16000:
        # crude resample to 16k for the encoder
        idx = (np.arange(int(len(arr) * 16000 / sr)) * (sr / 16000)).astype(int)
        arr = arr[np.clip(idx, 0, len(arr) - 1)]
    clf = _get_classifier()
    device = next(clf.parameters()).device
    t = torch.tensor(arr, dtype=torch.float32, device=device).unsqueeze(0)
    with torch.no_grad():
        emb = clf.encode_batch(t)
    return emb.squeeze().cpu().numpy()


def _smooth(labels: list) -> list:
    # Snap only isolated single-chunk blips (likely mid-sentence flips).
    # No majority vote: it erases genuine short turns.
    out = list(labels)
    for i in range(1, len(out) - 1):
        if out[i - 1] == out[i + 1] != out[i]:
            out[i] = out[i - 1]
    return out


def session_map(paths: list) -> dict:
    """Cluster all chunk embeddings into speakers 1..3 with smoothing."""
    if not paths:
        return {}
    embs = []
    for p in paths:
        try:
            embs.append(embed_wav(p))
        except Exception:
            embs.append(None)
    valid = [(p, e) for p, e in zip(paths, embs) if e is not None]
    if not valid:
        return {p: "1" for p in paths}
    # greedy agglomerative on cosine distance, cap 3
    clusters: list = []  # list of (centroid, count, label)
    assign: dict = {}
    for p, e in valid:
        best, best_d = None, 0.35
        for idx, (c, n, lab) in enumerate(clusters):
            d = 1.0 - float(np.dot(e, c) / ((np.linalg.norm(e) or 1) * (np.linalg.norm(c) or 1)))
            if d < best_d:
                best, best_d = idx, d
        if best is None and len(clusters) < 3:
            lab = str(len(clusters) + 1)
            clusters.append((e, 1, lab))
            assign[p] = lab
        elif best is None:
            # nearest of existing
            ds = [
                1.0 - float(np.dot(e, c) / ((np.linalg.norm(e) or 1) * (np.linalg.norm(c) or 1)))
                for c, _, _ in clusters
            ]
            bi = int(np.argmin(ds))
            c, n, lab = clusters[bi]
            clusters[bi] = ((c * n + e) / (n + 1), n + 1, lab)
            assign[p] = lab
        else:
            c, n, lab = clusters[best]
            clusters[best] = ((c * n + e) / (n + 1), n + 1, lab)
            assign[p] = lab
    ordered = [assign.get(p, "1") for p in paths]
    smoothed = _smooth(ordered)
    return {p: lab for p, lab in zip(paths, smoothed)}
