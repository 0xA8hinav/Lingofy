"""8s chunker: wav-native split, ffmpeg path for video when available."""

import shutil
import subprocess
import wave
from pathlib import Path

CHUNK_MS = 8000
MAX_MS = 9500


def _split_wav(wav_path: str, out_dir: str, session_id: str) -> list[dict]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    chunks: list[dict] = []
    with wave.open(wav_path, "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        n_frames = w.getnframes()
        frames_per_chunk = int(framerate * CHUNK_MS / 1000)
        seq = 0
        pos = 0
        while pos < n_frames:
            take = min(frames_per_chunk, n_frames - pos)
            w.setpos(pos)
            data = w.readframes(take)
            chunk_id = f"{session_id}_{seq:04d}"
            wav_out = out / f"{chunk_id}.wav"
            with wave.open(str(wav_out), "wb") as o:
                o.setnchannels(n_channels)
                o.setsampwidth(sampwidth)
                o.setframerate(framerate)
                o.writeframes(data)
            start_ms = int(pos / framerate * 1000)
            end_ms = int((pos + take) / framerate * 1000)
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "seq": seq,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "wav_path": str(wav_out),
                }
            )
            pos += take
            seq += 1
    return chunks


def chunk_video_to_wav(src_mp4: str, out_dir: str, session_id: str) -> list[dict]:
    src = Path(src_mp4)
    if not src.exists():
        raise FileNotFoundError(src_mp4)
    if src.suffix.lower() == ".wav":
        return _split_wav(str(src), out_dir, session_id)
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg missing, cannot chunk video")
    tmp_wav = Path(out_dir) / f"{session_id}_full.wav"
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(tmp_wav)],
        check=True,
        capture_output=True,
    )
    return _split_wav(str(tmp_wav), out_dir, session_id)
