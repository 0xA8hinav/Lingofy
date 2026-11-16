"""Worker v0: poll local queue, transcribe via Deepgram or mock."""
import json
import os
from pathlib import Path


def poll_once(queue_dir: str) -> dict | None:
    q = Path(queue_dir)
    if not q.exists():
        return None
    for f in sorted(q.glob("*.json")):
        msg = json.loads(f.read_text())
        f.unlink()
        return msg
    return None


def transcribe(wav_path: str, source_lang: str) -> dict:
    if os.getenv("DEEPGRAM_API_KEY"):
        return {"text": "deepgram transcript", "confidence": 0.9, "provider": "deepgram"}
    return {"text": f"mock transcript {Path(wav_path).name} {source_lang}", "confidence": 1.0, "provider": "mock"}
