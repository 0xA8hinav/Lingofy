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
        seg = samples[i:i + step]
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


def make_bed(src_wav: str, out_wav: str, duck: float = 0.10) -> str:
    """Write a bed wav: original audio with its speech ducked, ambience kept."""
    samples, sr = _read_wav(src_wav)
    mask = _dilate(speech_mask(samples, sr))
    step = max(1, int(sr * FRAME_MS / 1000))
    out = []
    for idx, s in enumerate(samples):
        fi = idx // step
        g = duck if (fi < len(mask) and mask[fi]) else 1.0
        out.append(int(s * g))
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
        ["ffmpeg", "-y", "-i", voice_wav, "-i", bed_wav,
         "-filter_complex",
         f"[0:a]volume=1.0[v];[1:a]volume={bed_gain}[b];[v][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]",
         "-map", "[out]", "-ar", "44100", "-ac", "1", out_wav],
        check=True, capture_output=True,
    )
    return out_wav


def dubbed_with_background(chunk_wav: str, voice_wav: str, workdir: str, name: str) -> str:
    """One call: build bed from the chunk, mix the voice over it.

    Tries Demucs (GPU, model cached in-process) for true separation; falls
    back to the RMS ducking bed if the GPU is unavailable or busy.
    """
    Path(workdir).mkdir(parents=True, exist_ok=True)
    bed = str(Path(workdir) / f"{name}_bed.wav")
    mix = str(Path(workdir) / f"{name}_mix.wav")
    if not separate_demucs(chunk_wav, bed):
        make_bed(chunk_wav, bed)
    mix_voice_bed(voice_wav, bed, mix)
    return mix


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
