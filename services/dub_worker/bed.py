"""Background bed: keep music/SFX from the original, duck the original speech.

Cheap alternative to full source separation (Demucs CPU is ~1.7x realtime,
too slow for the demo). Uses an RMS speech mask:
  - speech regions  -> bed attenuated hard (original voice must not bleed)
  - non-speech      -> bed kept near full (music, ambience, SFX survive)

The dubbed voice is then mixed on top at full level, so the result keeps
background sound like a real dub instead of a silent-track replacement.
"""

import struct
import wave
from pathlib import Path

SR_TARGET = 44100
FRAME_MS = 50


def _read_wav(path: str):
    with wave.open(path, "rb") as w:
        n, sr, ch, sw = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("expected 16-bit pcm")
    vals = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    if ch == 2:
        mono = [(vals[i] + vals[i + 1]) / 2 for i in range(0, len(vals) - 1, 2)]
    else:
        mono = list(vals)
    return mono, sr


def speech_mask(samples, sr: int, frame_ms: int = FRAME_MS, floor_mult: float = 2.0) -> list[bool]:
    """Per-frame booleans: True where the original speaker is talking.

    Threshold floats above the noise floor (low percentile of frame energy),
    so quiet ambience/music stays False while speech stays True.
    """
    step = max(1, int(sr * frame_ms / 1000))
    rms = []
    for i in range(0, len(samples), step):
        seg = samples[i : i + step]
        if not seg:
            break
        rms.append((sum(s * s for s in seg) / len(seg)) ** 0.5)
    if not rms:
        return []
    ordered = sorted(rms)
    floor = ordered[max(0, int(len(ordered) * 0.1) - 1)] if len(ordered) > 1 else ordered[0]
    peak = max(rms) or 1.0
    thresh = max(peak * 0.03, floor * floor_mult)
    return [r > thresh for r in rms]


def _dilate(mask, k=3):
    out = list(mask)
    for i in range(len(mask)):
        if mask[i]:
            for j in range(max(0, i - k), min(len(mask), i + k + 1)):
                out[j] = True
    return out


def make_bed(src_wav: str, out_wav: str, duck: float = 0.08, fade_ms: int = 150) -> str:
    """Write a bed wav: original audio with its speech ducked, ambience kept.

    Gain moves on smoothstep ramps (default 150 ms) so ducking fades in
    and out instead of clicking. Edge samples are untouched so consecutive
    chunk beds join seamlessly.
    """
    samples, sr = _read_wav(src_wav)
    mask = _dilate(speech_mask(samples, sr))
    step = max(1, int(sr * FRAME_MS / 1000))
    n_frames = max(1, (len(samples) + step - 1) // step)
    while len(mask) < n_frames:
        mask.append(False)
    target = [duck if m else 1.0 for m in mask[:n_frames]]
    fade_n = max(1, round(fade_ms / FRAME_MS))
    gain = list(target)
    # smooth only interior transitions; leave first/last frames hard for clean joins
    for i in range(1, n_frames - 1):
        lo = max(1, i - fade_n)
        hi = min(n_frames - 2, i + fade_n)
        window = target[lo : hi + 1]
        if min(window) < max(window):
            t = (i - lo) / max(1, hi - lo)
            a, b = target[lo], target[hi]
            s = t * t * (3 - 2 * t)  # smoothstep
            gain[i] = a + (b - a) * s
        else:
            gain[i] = target[i]
    out = []
    for idx, s in enumerate(samples):
        fi = min(idx // step, n_frames - 1)
        nxt = min(fi + 1, n_frames - 1)
        frac = (idx % step) / step
        g = gain[fi] * (1 - frac) + gain[nxt] * frac
        out.append(int(max(-32768, min(32767, s * g))))
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<" + "h" * len(out), *out))
    return out_wav


def mix_voice_bed(voice_wav: str, bed_wav: str, out_wav: str, bed_gain: float = 0.9) -> str:
    """Mix dubbed voice (full) over bed (bed_gain). Duration follows the voice."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        shutil.copy(voice_wav, out_wav)
        return out_wav
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            voice_wav,
            "-i",
            bed_wav,
            "-filter_complex",
            f"[0:a]volume=1.0[v];[1:a]volume={bed_gain}[b];[v][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]",
            "-map",
            "[out]",
            "-ar",
            "44100",
            "-ac",
            "1",
            out_wav,
        ],
        check=True,
        capture_output=True,
    )
    return out_wav


def mix_voice_over_gaps(voice_wav: str, src_chunk_wav: str, out_wav: str, fade_ms: int = 60) -> str:
    """Dubbed voice where the source has speech, ORIGINAL audio everywhere else.

    No bed under speech at all — the original voice can never bleed through.
    Gaps keep the real room tone / music / SFX. Short crossfades so joins
    never click. The voice track must already be fitted to the chunk length.
    """
    import shutil
    import subprocess
    import tempfile

    voice, sr = _read_wav(voice_wav)
    try:
        src, sr2 = _read_wav(src_chunk_wav)
    except Exception:
        shutil.copy(voice_wav, out_wav)
        return out_wav
    if sr2 != sr and shutil.which("ffmpeg"):
        tmp = tempfile.mktemp(suffix=".wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", src_chunk_wav, "-ac", "1", "-ar", str(sr), tmp], check=True, capture_output=True
        )
        src, _ = _read_wav(tmp)
    n = max(len(voice), len(src))
    voice += [0] * (n - len(voice))
    src += [0] * (n - len(src))
    mask = _dilate(speech_mask(src, sr), k=2)
    step = max(1, int(sr * FRAME_MS / 1000))
    n_frames = max(1, (n + step - 1) // step)
    while len(mask) < n_frames:
        mask.append(False)
    fade = max(1, int(sr * fade_ms / 1000))
    out = list(src)
    i = 0
    while i < n:
        fi = min(i // step, n_frames - 1)
        if not mask[fi]:
            i += 1
            continue
        j = i
        while j < n and mask[min(j // step, n_frames - 1)]:
            j += 1
        for k in range(i, j):  # crossfade the edges of each speech span
            w = 1.0
            dl, dr = k - i, j - 1 - k
            if dl < fade:
                w = dl / fade
            if dr < fade:
                w = min(w, dr / fade)
            out[k] = int(max(-32768, min(32767, voice[k] * w + src[k] * (1 - w))))
        i = j
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<" + "h" * len(out), *out))
    return out_wav


def dubbed_with_background(chunk_wav: str, voice_wav: str, workdir: str, name: str) -> str:
    """Voice where the source speaks, original audio in the gaps (no bed under speech)."""
    Path(workdir).mkdir(parents=True, exist_ok=True)
    return mix_voice_over_gaps(voice_wav, chunk_wav, str(Path(workdir) / f"{name}_mix.wav"))


_DEMUCS = None
_DEMUCS_DEVICE = None


def _load_demucs():
    global _DEMUCS, _DEMUCS_DEVICE
    if _DEMUCS is not None:
        return _DEMUCS, _DEMUCS_DEVICE
    import torch
    from demucs.pretrained import get_model

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = get_model("htdemucs")
    model.to(dev)
    model.eval()
    _DEMUCS, _DEMUCS_DEVICE = model, dev
    return _DEMUCS, _DEMUCS_DEVICE


def separate_demucs(src_wav: str, out_bed: str) -> bool:
    """Isolate accompaniment (no vocals) from src_wav. True on success."""
    import os

    if os.getenv("LINGOFY_SEPARATE", "demucs") == "off":
        return False
    try:
        import torch
        from demucs.apply import apply_model

        model, dev = _load_demucs()
        samples, sr = _read_wav(src_wav)
        if sr != model.samplerate:
            # demucs expects 44.1k stereo; our chunks are 16k mono
            samples = _resample(samples, sr, model.samplerate)
            sr = model.samplerate
        wav = torch.tensor(samples, dtype=torch.float32) / 32768.0
        wav = wav.unsqueeze(0).repeat(2, 1).unsqueeze(0)  # (1, 2, T)
        wav = wav.to(dev)
        with torch.no_grad():
            stems = apply_model(model, wav, split=True, overlap=0.1)[0]
        # stems: [drums, bass, other, vocals]; bed = everything but vocals
        acc = stems[0] + stems[1] + stems[2]
        acc = acc.mean(dim=0).clamp(-1, 1)
        pcm = (acc.cpu().numpy() * 32767).astype("int16")
        import wave as _w

        with _w.open(out_bed, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return True
    except Exception:
        return False


def _resample(samples, src_sr: int, dst_sr: int):
    if src_sr == dst_sr or not samples:
        return samples
    step = src_sr / dst_sr
    n = int(len(samples) / step)
    return [samples[min(len(samples) - 1, int(i * step))] for i in range(n)]
