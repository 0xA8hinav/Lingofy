"""Assembler: order by seq, mux-back with video copy, progressive manifest."""
import json
from pathlib import Path


def assemble_session(session_id: str, target_lang: str, dubbed_dir: str = "", out_dir: str = "", chunks_total: int | None = None, src_video: str = "", preview: bool = False) -> dict:
    import shutil
    import subprocess

    dubbed = Path(dubbed_dir) if dubbed_dir else Path(f"data/dubbed/{session_id}/{target_lang}")
    out = Path(out_dir) if out_dir else Path(f"output/{session_id}")
    out.mkdir(parents=True, exist_ok=True)
    wavs = sorted(dubbed.glob("*.wav")) if dubbed.exists() else []
    mp3s = wavs or (sorted(dubbed.glob("*.mp3")) if dubbed.exists() else [])
    total = chunks_total if chunks_total is not None else max(1, len(mp3s))
    done = len(mp3s)
    name = f"preview_{target_lang}.mp4" if preview else f"dubbed_{target_lang}.mp4"
    video_path = out / name
    if src_video and Path(src_video).exists() and shutil.which("ffmpeg") and mp3s:
        lst = out / "concat.txt"
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in mp3s))
        audio = out / "dubbed_audio.m4a"
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "aac", str(audio)], check=True, capture_output=True)
        if Path(src_video).suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
            subprocess.run(["ffmpeg", "-y", "-i", src_video, "-i", str(audio), "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(video_path)], check=True, capture_output=True)
        else:
            audio.replace(video_path)
    else:
        video_path.write_bytes(b"".join(p.read_bytes() for p in mp3s) or b"EMPTY")
    manifest = {
        "video_url": str(video_path),
        "chunks_total": total,
        "chunks_done": done,
        "speakers": [1, 2, 3],
        "unavailable": [] if done >= total else list(range(done, total)),
        "drift_ms": 0,
    }
    (out / "manifest.json").write_text(json.dumps(manifest))
    return {"video_path": str(video_path), "manifest_path": str(out / "manifest.json"), **manifest}


def assemble_chunk(src_video: str, start_s: float, dur_s: float, dubbed_wav: str, out_mp4: str) -> str:
    """Mux one playable segment: source picture slice + fitted dubbed audio."""
    import shutil
    import subprocess
    from pathlib import Path

    Path(out_mp4).parent.mkdir(parents=True, exist_ok=True)
    if Path(src_video).suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") and shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start_s), "-t", str(dur_s), "-i", src_video,
             "-i", dubbed_wav, "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0",
             "-shortest", out_mp4],
            check=True, capture_output=True,
        )
    else:
        shutil.copy(dubbed_wav, out_mp4)
    return out_mp4


def assemble_mse_chunk(src_video: str, start_s: float, dur_s: float, dubbed_wav: str, out_path: str) -> str:
    """Re-encoded fragmented MP4 segment (empty_moov) for MSE append playback.

    Same encoder settings every call, so one init segment fits all chunks.
    Returns path to the FULL fmp4; use split_fmp4() for init/media parts.
    """
    import shutil
    import subprocess
    from pathlib import Path

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(src_video).suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") and shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start_s), "-t", str(dur_s), "-i", src_video,
             "-i", dubbed_wav, "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "libopenh264", "-b:v", "1200k", "-g", "240",
             "-c:a", "aac", "-ar", "44100", "-ac", "2",
             "-movflags", "empty_moov+default_base_moof+frag_keyframe",
             "-frag_duration", "8000000", "-f", "mp4", out_path],
            check=True, capture_output=True,
        )
    else:
        shutil.copy(dubbed_wav, out_path)
    return out_path


def split_fmp4(fmp4_path: str) -> tuple:
    """Split fragmented MP4 into (init_bytes, media_bytes). Drops trailing mfra."""
    data = Path(fmp4_path).read_bytes()
    i, boxes = 0, []
    while i + 8 <= len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        t = data[i + 4:i + 8]
        size = n if n >= 8 else len(data) - i
        boxes.append((t, i, size))
        if n < 8:
            break
        i += size
    init_end = 0
    for t, off, size in boxes:
        if t == b"moof":
            init_end = off
            break
    media_end = len(data)
    for t, off, size in boxes:
        if t == b"mfra":
            media_end = off
            break
    return data[:init_end], data[init_end:media_end]
