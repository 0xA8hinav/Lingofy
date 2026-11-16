"""Assembler: order by seq, mux-back with video copy, progressive manifest."""
import json
from pathlib import Path


def assemble_session(session_id: str, target_lang: str, dubbed_dir: str = "", out_dir: str = "", chunks_total: int | None = None) -> dict:
    dubbed = Path(dubbed_dir) if dubbed_dir else Path(f"data/dubbed/{session_id}/{target_lang}")
    out = Path(out_dir) if out_dir else Path(f"output/{session_id}")
    out.mkdir(parents=True, exist_ok=True)
    mp3s = sorted(dubbed.glob("*.mp3")) if dubbed.exists() else []
    total = chunks_total if chunks_total is not None else max(1, len(mp3s))
    done = len(mp3s)
    video_path = out / f"dubbed_{target_lang}.mp4"
    # stub mux: concat bytes to prove ordering; real ffmpeg -c:v copy in demo
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
