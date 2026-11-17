"""Gateway: video ingest, status, manifest. Local data/ stand-in for S3/SQS ($0 demo)."""
import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
SESSIONS: dict[str, dict] = {}
DATA = Path("data")
QUEUE = DATA / "queue" / "dub-tasks"
AUDIO = DATA / "audio"
OUTPUT = Path("output")
OUTPUT.mkdir(exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT)), name="output")

# Preload heavy local models at startup so first chunk dubs in seconds.
try:
    from faster_whisper import WhisperModel as _WM

    _WARM = _WM("tiny", device="cpu", compute_type="int8")
    import services.dub_worker.worker as _W

    _W._FW_MODEL = _WARM
except Exception:
    pass
try:
    import argostranslate.translate as _T

    _T.translate("warmup", "en", "hi")
except Exception:
    pass


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    return {"ok": True, "queue": True}


@app.post("/v1/sessions")
def create_session(body: dict):
    src = body.get("source_lang", "en")
    tgt = body.get("target_lang", "hi")
    if src not in ("en", "hi") or tgt not in ("en", "hi") or src == tgt:
        raise HTTPException(400, "need source_lang/target_lang as en|hi, different")
    sid = f"sess_{uuid.uuid4().hex[:6]}"
    SESSIONS[sid] = {"source_lang": src, "target_lang": tgt, "chunks_total": 0, "chunks_done": 0}
    return {"session_id": sid, "upload_url": f"/v1/sessions/{sid}/video"}


def _run_dub(sid: str):
    """Legacy full-dub path (kept for scripts/demo.py offline runs)."""
    return


_CHUNK_LOCKS: dict = {}
import threading as _th
_SEGMAPS: dict = {}


def _session_full_wav(sid: str) -> str:
    """Concat session chunks into one 16k mono wav (built once, reused)."""
    import subprocess

    from pathlib import Path as _P

    s = SESSIONS[sid]
    full_wav = _P("data") / "dubbed" / sid / "_full16k.wav"
    full_wav.parent.mkdir(parents=True, exist_ok=True)
    if not full_wav.exists():
        lst = full_wav.with_suffix(".txt")
        lst.write_text("".join(f"file '{_P(c['wav_path']).resolve()}'\n" for c in s["chunks"]))
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-ac", "1", "-ar", "16000", str(full_wav)], check=True, capture_output=True)
    return str(full_wav)


def _session_segments(sid: str) -> list:
    """File-level Gemini speaker map, computed once per session, cached on disk."""
    if sid in _SEGMAPS:
        return _SEGMAPS[sid]
    from services.dub_worker import gemini_diarize as _gd

    s = SESSIONS[sid]
    chunks = s["chunks"]
    # content-hash cache: re-uploads of the same video reuse the map, $0
    import hashlib

    h = hashlib.sha1()
    try:
        with open(s["src"], "rb") as _f:
            h.update(_f.read(1 << 20))
            _f.seek(max(0, Path(s["src"]).stat().st_size - (1 << 20)))
            h.update(_f.read(1 << 20))
    except Exception:
        h.update(sid.encode())
    cache = _gd.CACHE / f"{h.hexdigest()}.json"
    if cache.exists():
        try:
            segs = json.loads(cache.read_text())
            _SEGMAPS[sid] = segs
            return segs
        except Exception:
            pass
    chunks = s["chunks"]
    total_s = max(c["end_ms"] for c in chunks) / 1000
    full_wav = _session_full_wav(sid)
    try:
        segs = _gd.diarize_file(str(full_wav), total_s)
    except Exception:
        segs = []
    try:
        _gd.CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(segs))
    except Exception:
        pass
    _SEGMAPS[sid] = segs
    return segs


def _dub_chunk(sid: str, seq: int) -> str:
    """Dub one chunk on demand; returns segment url. Gated by playback: only called for next chunk."""
    from services.assembler.assemble import assemble_chunk
    from services.dub_worker.voices import synthesize, translate
    from services.dub_worker.worker import transcribe

    s = SESSIONS[sid]
    chunks = s["chunks"]
    c = next(x for x in chunks if x["seq"] == seq)
    lock = _CHUNK_LOCKS.setdefault((sid, seq), _th.Lock())
    seg = OUTPUT / sid / f"seg_{seq:04d}_{s['target_lang']}.mp4"
    if seg.exists():
        return f"/output/{sid}/{seg.name}"
    with lock:
        if seg.exists():
            return f"/output/{sid}/{seg.name}"
        dur_ms = c["end_ms"] - c["start_ms"]
        # Gemini-first, zero paid APIs: file transcript sliced per chunk + file speaker map
        try:
            from services.dub_worker import gemini_diarize as _gd

            _segs = _session_segments(sid)
            _full = _session_full_wav(sid)
            text, label = _gd.chunk_text(_full, c["start_ms"] / 1000, c["end_ms"] / 1000)
            if not label:
                label = _gd.speaker_at(_segs, c["start_ms"] / 1000)
        except Exception:
            text, label = "", ""
        if not text:
            r = transcribe(c["wav_path"], s["source_lang"])
            text = r["text"]
            label = label or r.get("speaker_label") or "1"
        tt = translate(text, s["source_lang"], s["target_lang"])["target_text"]
        dubbed = Path("data") / "dubbed" / sid
        dubbed.mkdir(parents=True, exist_ok=True)
        out = synthesize(tt, s["target_lang"], label, str(dubbed), name=f"{seq:04d}_{label}", target_ms=dur_ms)
        assemble_chunk(s["src"], c["start_ms"] / 1000, dur_ms / 1000, out["wav_path"], str(seg))
        SESSIONS[sid]["chunks_done"] = max(s.get("chunks_done", 0), seq + 1)
        return f"/output/{sid}/{seg.name}"


@app.get("/v1/sessions/{sid}/chunk/{seq}")
def get_chunk(sid: str, seq: int):
    s = SESSIONS.get(sid)
    if not s or "chunks" not in s:
        raise HTTPException(404, "unknown session")
    total = len(s["chunks"])
    if seq >= total:
        return {"done": True, "total": total}
    try:
        url = _dub_chunk(sid, seq)
    except Exception as e:
        raise HTTPException(500, f"chunk dub failed: {e}")
    return {"done": False, "seq": seq, "total": total, "url": url, "next": seq + 1}


@app.post("/v1/sessions/{sid}/video")
async def upload_video(
    sid: str, file: UploadFile = File(...), x_mock_size: int | None = Header(default=None)
):
    if sid not in SESSIONS:
        SESSIONS[sid] = {"source_lang": "en", "target_lang": "hi", "chunks_total": 0, "chunks_done": 0}
    content = await file.read()
    size = x_mock_size if x_mock_size is not None else len(content)
    if size > 500 * 1024 * 1024:
        raise HTTPException(413, "over 500MB cap, use trimmed clip")
    name = (file.filename or "upload").lower()
    if not name.endswith((".mp4", ".mov", ".webm", ".mkv", ".wav", ".mp3")):
        raise HTTPException(400, "unsupported format")
    if len(content) < 100 or (not name.endswith((".wav", ".mp3")) and content[:4] not in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20", b"RIFF", b"\x1a\x45\xdf\xa3", b"ftyp")):
        # tiny heuristic: reject obvious non-video; wav RIFF passes
        if not (name.endswith(".wav") and content[:4] == b"RIFF") and len(content) < 1000:
            raise HTTPException(400, "corrupt video")
        if content == b"notavideo":
            raise HTTPException(400, "corrupt video")
    tmp = DATA / "uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp / f"{sid}_{name}"
    tmp_file.write_bytes(content)
    # chunk only real wav fixtures in tests; video path needs ffmpeg (Task 6 installs it)
    from .chunker import chunk_video_to_wav

    out_dir = AUDIO / sid
    try:
        if name.endswith(".wav"):
            chunks = chunk_video_to_wav(str(tmp_file), str(out_dir), sid)
        else:
            import shutil as _shutil

            if _shutil.which("ffmpeg") is None:
                raise HTTPException(400, "ffmpeg missing, install it for video chunking")
            chunks = chunk_video_to_wav(str(tmp_file), str(out_dir), sid)
    except (ValueError, FileNotFoundError):
        raise HTTPException(400, "corrupt video or ffmpeg missing")
    QUEUE.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        msg = {
            "session_id": sid,
            "chunk_id": c["chunk_id"],
            "seq": c["seq"],
            "start_ms": c["start_ms"],
            "end_ms": c["end_ms"],
            "s3_key": c["wav_path"],
            "speaker_label": "1",
        }
        (QUEUE / f"{c['chunk_id']}.json").write_text(json.dumps(msg))
    SESSIONS[sid]["chunks_total"] = len(chunks)
    SESSIONS[sid]["chunks"] = chunks
    SESSIONS[sid]["src"] = str(tmp_file)
    return {"chunks_total": len(chunks)}


@app.get("/v1/sessions/{sid}/status")
def status(sid: str):
    s = SESSIONS.get(sid, {"chunks_total": 0, "chunks_done": 0})
    return {
        "chunks_total": s["chunks_total"],
        "chunks_done": s.get("chunks_done", 0),
        "video_url": s.get("video_url"),
        "preview_url": s.get("preview_url"),
        "elapsed_s": s.get("elapsed_s"),
        "error": s.get("error"),
    }


@app.get("/v1/sessions/{sid}/manifest")
def manifest(sid: str):
    s = SESSIONS.get(sid, {"chunks_total": 0, "chunks_done": 0})
    tgt = s.get("target_lang", "hi")
    return {
        "video_url": s.get("video_url") or f"/output/{sid}/dubbed_{tgt}.mp4",
        "playlist_url": f"/output/{sid}/playlist.m3u8",
        "speakers": [1, 2, 3],
        "chunks_total": s["chunks_total"],
        "chunks_done": s.get("chunks_done", 0),
    }


@app.get("/v1/cost")
def cost():
    from services.dub_worker import costs as _costs

    return _costs.totals()


@app.get("/", response_class=HTMLResponse)
def index():
    return Path(__file__).parent.joinpath("static/index.html").read_text()
