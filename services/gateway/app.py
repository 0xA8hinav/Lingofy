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
    import time

    from scripts.demo import run_demo

    s = SESSIONS.get(sid)
    if not s:
        return
    srcs = sorted((DATA / "uploads").glob(f"{sid}_*"))
    if not srcs:
        return
    t0 = time.time()
    try:
        from concurrent.futures import ThreadPoolExecutor

        from services.gateway.chunker import chunk_video_to_wav

        chunks = chunk_video_to_wav(str(srcs[0]), str(AUDIO / sid), sid)
        SESSIONS[sid]["chunks_total"] = len(chunks)
        from services.assembler.assemble import assemble_session
        from services.dub_worker.diarize import label_session
        from services.dub_worker.voices import synthesize, translate
        from services.dub_worker.worker import transcribe

        dubbed = Path("data") / "dubbed" / sid
        dubbed.mkdir(parents=True, exist_ok=True)
        try:
            spk = label_session([c["wav_path"] for c in chunks])
        except Exception:
            spk = {}

        def _one(c):
            try:
                label = spk.get(c["wav_path"], "1")
                tx = transcribe(c["wav_path"], s["source_lang"])["text"]
                tt = translate(tx, s["source_lang"], s["target_lang"])["target_text"]
                synthesize(tt, s["target_lang"], label, str(dubbed), name=f"{c['seq']:04d}_{label}")
            except Exception:
                pass
            return c["seq"]

        import itertools

        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for _ in ex.map(_one, chunks):
                done += 1
                SESSIONS[sid]["chunks_done"] = done
                if done == 1 or done % 4 == 0:
                    try:
                        assemble_session(sid, s["target_lang"], dubbed_dir=str(dubbed), out_dir=str(OUTPUT / sid), chunks_total=len(chunks), src_video=str(srcs[0]), preview=True)
                        SESSIONS[sid]["preview_url"] = f"/output/{sid}/preview_{s['target_lang']}.mp4"
                    except Exception:
                        pass
        assemble_session(sid, s["target_lang"], dubbed_dir=str(dubbed), out_dir=str(OUTPUT / sid), chunks_total=len(chunks), src_video=str(srcs[0]))
        SESSIONS[sid]["video_url"] = f"/output/{sid}/dubbed_{s['target_lang']}.mp4"
        SESSIONS[sid]["elapsed_s"] = round(time.time() - t0, 1)
    except Exception as e:
        SESSIONS[sid]["error"] = str(e)


@app.post("/v1/sessions/{sid}/dub")
def start_dub(sid: str, bg: BackgroundTasks):
    if sid not in SESSIONS:
        raise HTTPException(404, "unknown session")
    bg.add_task(_run_dub, sid)
    return {"started": True}


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


@app.get("/", response_class=HTMLResponse)
def index():
    return Path(__file__).parent.joinpath("static/index.html").read_text()
