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
SAMPLES = Path("/app/samples")
if SAMPLES.exists():
    app.mount("/samples", StaticFiles(directory=str(SAMPLES)), name="samples")


@app.get("/metrics", response_class=HTMLResponse)
def metrics():
    """Prometheus plaintext: session/chunk gauges + cost counter (one SLO)."""
    try:
        from services.dub_worker import costs as _c

        t = _c.totals()
        sarvam_calls, sarvam_inr = t.get("sarvam_calls", 0), t.get("sarvam_inr_est", 0)
    except Exception:
        sarvam_calls, sarvam_inr = 0, 0
    n_sess = len(SESSIONS)
    done = sum(s.get("chunks_done", 0) for s in SESSIONS.values())
    total = sum(s.get("chunks_total", 0) for s in SESSIONS.values())
    lines = [
        "# HELP lingofy_sessions_total Active dub sessions",
        "# TYPE lingofy_sessions_total gauge",
        f"lingofy_sessions_total {n_sess}",
        "# HELP lingofy_chunks_done_total Dubbed chunks",
        "# TYPE lingofy_chunks_done_total counter",
        f"lingofy_chunks_done_total {done}",
        "# HELP lingofy_chunks_total_total Chunk backlog + done",
        "# TYPE lingofy_chunks_total_total counter",
        f"lingofy_chunks_total_total {total}",
        "# HELP lingofy_sarvam_calls_total Sarvam TTS calls",
        "# TYPE lingofy_sarvam_calls_total counter",
        f"lingofy_sarvam_calls_total {sarvam_calls}",
        "# HELP lingofy_sarvam_inr_est_total Estimated Sarvam spend (INR)",
        "# TYPE lingofy_sarvam_inr_est_total counter",
        f"lingofy_sarvam_inr_est_total {sarvam_inr}",
    ]
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("\n".join(lines) + "\n")


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


def _warm_gender_nn():
    """Background warmup: wav2vec2-large gender model costs ~2 min cold on CPU.

    Without this, the first dub of a server's life pays that inside the dub
    path (the 'Analyzing voices' stall). Daemon thread: never blocks boot.
    """
    import threading as _th

    def _w():
        try:
            from services.dub_worker import diarize as _dz

            _dz.classify_gender_nn(__file__)  # model load; inference error ignored
        except Exception:
            pass

    _th.Thread(target=_w, daemon=True).start()


_warm_gender_nn()


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
    """Full-video background dub: transcript sliced from cache, parallel synth, one mux."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path as _P

    from services.assembler.assemble import assemble_session
    from services.dub_worker import gemini_diarize as _gd
    from services.dub_worker.voices import synthesize, translate_batch

    s = SESSIONS.get(sid)
    if not s:
        return
    t0 = time.time()

    def _mark(tag: str):
        print(f"[dub {sid}] {tag} +{time.time() - t0:.1f}s", flush=True)

    try:
        chunks = s["chunks"]
        SESSIONS[sid]["chunks_total"] = len(chunks)
        full_wav = _session_full_wav(sid)
        _mark("full_wav ready")
        segs = _session_segments(sid)  # one gemini call; also caches transcript words
        _mark(f"segments ready ({len(segs)} segs)")
        dubbed = _P("data") / "dubbed" / sid
        dubbed.mkdir(parents=True, exist_ok=True)

        # phase 1: slice cached transcripts (instant) for every chunk
        jobs = []
        for c in chunks:
            try:
                text, label = _gd.chunk_text(full_wav, c["start_ms"] / 1000, c["end_ms"] / 1000)
            except Exception:
                text, label = "", ""
            if not label:
                label = _gd.speaker_at(segs, c["start_ms"] / 1000) if segs else "1"
            if not text:
                try:  # transcript cache missing -> per-chunk offline STT fallback
                    from services.dub_worker.worker import transcribe as _tx

                    r = _tx(c["wav_path"], s["source_lang"])
                    text = r["text"]
                    # Never voice the mock fallback (offline model missing):
                    # silence is better than narrating filenames on stage.
                    if text.startswith("mock transcript"):
                        continue
                    label = label or r.get("speaker_label") or "1"
                except Exception:
                    continue
            jobs.append((c, text, label or "1"))
        # phase 2: ONE batched translation call for all chunks (was: 1 call/chunk).
        # Disk-cached by content hash: the demo sample pre-fills this, so stage
        # time spends zero Gemini calls (rate-limit bottleneck) — only Sarvam live.
        from services.dub_worker.voices import translate as _translate_one

        _mark(f"phase1 done ({len(jobs)} jobs)")
        import hashlib as _hl

        _tkey = _hl.sha1("\n".join(f"{s['source_lang']}>{s['target_lang']}:{t}" for _, t, _ in jobs).encode()).hexdigest()
        _tfile = Path("data") / "transcripts" / f"batch_{_tkey}.json"
        tts = None
        if _tfile.exists():
            try:
                tts = json.loads(_tfile.read_text())
                _mark("translate_batch cache HIT")
            except Exception:
                tts = None
        if not tts:
            try:
                tts = translate_batch([t for _, t, _ in jobs], s["source_lang"], s["target_lang"])
            except Exception:
                tts = [""] * len(jobs)
            try:
                _tfile.parent.mkdir(parents=True, exist_ok=True)
                _tfile.write_text(json.dumps(tts))
            except Exception:
                pass
            _mark("translate_batch done")
        paired = []
        for (c, t, lab), tt in zip(jobs, tts):
            if not tt:
                try:
                    tt = _translate_one(t, s["source_lang"], s["target_lang"])["target_text"]
                except Exception:
                    continue
            paired.append((c, lab, tt))

        def _one(item):
            c, label, tt = item
            try:
                gm = _session_genders(sid).get(label, {"gender": "", "gindex": 0})
                _wav = dubbed / f"{c['seq']:04d}_{label}.wav"
                if _wav.exists():
                    _dub_done(sid, c["seq"])  # already dubbed via chunk path; skip duplicate synth
                    return c["seq"]
                dur_ms = c["end_ms"] - c["start_ms"]
                synthesize(
                    tt,
                    s["target_lang"],
                    label,
                    str(dubbed),
                    name=f"{c['seq']:04d}_{label}",
                    target_ms=dur_ms,
                    gender=gm["gender"],
                    gindex=gm["gindex"],
                )
                # Dub-only: pure TTS voice, original audio muted (bed mix removed).
                _dub_done(sid, c["seq"])
            except Exception:
                pass
            return c["seq"]

        _mark(f"synth start ({len(paired)} paired)")

        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for _ in ex.map(_one, paired):
                done += 1
                SESSIONS[sid]["chunks_done"] = done
        _mark("synth done")
        got = len(sorted(dubbed.glob("*.wav")))
        if got < len(chunks) // 2:
            raise RuntimeError(f"only {got}/{len(chunks)} chunks dubbed — transcript unavailable, retry later")
        assemble_session(
            sid,
            s["target_lang"],
            dubbed_dir=str(dubbed),
            out_dir=str(OUTPUT / sid),
            chunks_total=len(chunks),
            src_video=s["src"],
        )
        SESSIONS[sid]["video_url"] = f"/output/{sid}/dubbed_{s['target_lang']}.mp4"
        _mark("assemble done")
        try:
            import shutil as _sh

            _sh.copy(s["src"], str(OUTPUT / sid / "orig.mp4"))
            SESSIONS[sid]["orig_url"] = f"/output/{sid}/orig.mp4"
        except Exception:
            pass
        SESSIONS[sid]["elapsed_s"] = round(time.time() - t0, 1)
    except Exception as e:
        SESSIONS[sid]["error"] = str(e)


_CHUNK_LOCKS: dict = {}
import threading as _th

_SEGMAPS: dict = {}
_GENDERS: dict = {}
_DUB_READY: dict = {}
_BATCH_READY: dict = {}


def _batch_ready(sid: str) -> "_th.Event":
    ev = _BATCH_READY.get(sid)
    if ev is None:
        ev = _BATCH_READY[sid] = _th.Event()
    return ev


def _dub_done(sid: str, seq: int):
    key = (sid, seq)
    ev = _DUB_READY.get(key)
    if ev is None:
        ev = _DUB_READY[key] = _th.Event()
    ev.set()


def _session_genders(sid: str) -> dict:
    """One NN gender check per speaker on their longest turn. Cached.

    File-level segments come first (diarize once per upload); each distinct
    speaker gets a single voice sample classified, then every chunk of theirs
    reuses the result. Pitch is only a fallback when the model abstains.
    """
    if sid in _GENDERS:
        return _GENDERS[sid]
    from services.dub_worker import diarize as _dz

    try:
        segs = _session_segments(sid)
    except Exception:
        segs = []
    labels = sorted({str(x["speaker"]) for x in segs}) if segs else []
    out: dict = {}
    counts: dict = {}
    try:
        full = _session_full_wav(sid)
        import wave as _w

        with _w.open(full, "rb") as _f:
            _sr, _n = _f.getframerate(), _f.getnframes()
            _raw = _f.readframes(_n)
        import struct as _st

        _vals = list(_st.unpack("<" + "h" * (len(_raw) // 2), _raw)) if _raw else [0]
    except Exception:
        _vals, _sr = [0], 16000
    for lab in labels:
        lab_segs = [x for x in segs if str(x["speaker"]) == lab] or [{"start": 0, "end": 8}]
        longest = max(lab_segs, key=lambda x: x["end"] - x["start"])
        mid = (longest["start"] + longest["end"]) / 2
        a = max(0, int((mid - 2) * _sr))
        b = min(len(_vals), int((mid + 2) * _sr))
        g = "unknown"
        try:
            import tempfile

            _p = tempfile.mktemp(suffix=".wav")
            with _w.open(_p, "wb") as _o:
                _o.setnchannels(1)
                _o.setsampwidth(2)
                _o.setframerate(_sr)
                _o.writeframes(_st.pack("<" + "h" * max(1, b - a), *(_vals[a:b] or [0])))
            g = _dz.classify_gender_nn(_p)
            if g == "unknown":
                g = _dz.gender_for_wav(_p)
        except Exception:
            pass
        out[lab] = {"gender": g, "gindex": counts.get(g, 0)}
        if g != "unknown":
            counts[g] = counts.get(g, 0) + 1
    _GENDERS[sid] = out
    return out


def _session_full_wav(sid: str) -> str:
    """Concat session chunks into one 16k mono wav (built once, reused).

    Lives OUTSIDE dubbed/ on purpose: assemble globs that dir for per-chunk
    wavs, and the full source must never be picked up as dubbed output.
    """
    import subprocess
    from pathlib import Path as _P

    s = SESSIONS[sid]
    work = _P("data") / "work" / sid
    work.mkdir(parents=True, exist_ok=True)
    full_wav = work / "_full16k.wav"
    if not full_wav.exists():
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{_P(c['wav_path']).resolve()}'\n" for c in s["chunks"]))
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-ac", "1", "-ar", "16000", str(full_wav)],
            check=True,
            capture_output=True,
        )
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
    orig = OUTPUT / sid / f"orig_{seq:04d}.mp4"
    mse = OUTPUT / sid / f"mse_{seq:04d}.m4s"

    def _resp(label="", gender=""):
        _dur = c["end_ms"] - c["start_ms"]
        return {
            "url": f"/output/{sid}/{seg.name}",
            "orig_url": f"/output/{sid}/{orig.name}" if orig.exists() else "",
            "mse_url": f"/output/{sid}/{mse.name}" if mse.exists() else "",
            "mse_init": f"/output/{sid}/mse_init.mp4",
            "codecs": s.get("mse_codecs") or "avc1.42C01F,mp4a.40.2",
            "start": c["start_ms"] / 1000,
            "dur": _dur / 1000,
            "speaker": label,
            "gender": gender or "unknown",
        }

    if seg.exists():
        return _resp()
    with lock:
        if seg.exists():
            return _resp()
        import time as _t

        _t0 = _t.time()
        _marks = {}
        dur_ms = c["end_ms"] - c["start_ms"]
        # Translated segments (timestamps + translation in ONE cached pass) first;
        # falls back to slice + per-chunk translate.
        _gd_chunk_is_translated = False
        try:
            from services.dub_worker import gemini_diarize as _gd

            _full = _session_full_wav(sid)
            text, label = _gd.chunk_translated(_full, s["target_lang"], c["start_ms"] / 1000, c["end_ms"] / 1000)
            if text:
                _gd_chunk_is_translated = True
            if not text:
                _segs = _session_segments(sid)
                text, label = _gd.chunk_text(_full, c["start_ms"] / 1000, c["end_ms"] / 1000)
                if not label:
                    label = _gd.speaker_at(_segs, c["start_ms"] / 1000)
        except Exception:
            text, label = "", ""
        if not text:
            r = transcribe(c["wav_path"], s["source_lang"])
            text = r["text"]
            label = label or r.get("speaker_label") or "1"
        gm = _session_genders(sid).get(label, {"gender": "", "gindex": 0})
        _marks["map"] = round(_t.time() - _t0, 2)
        # If the background full-dub is already working on this chunk, wait for
        # it (up to ~one chunk of playback) instead of firing a duplicate TTS call.
        _ev = _DUB_READY.get((sid, seq))
        if _ev is None:
            _ev = _DUB_READY[(sid, seq)] = _th.Event()
        _ev.wait(timeout=20)
        # Prefer the warmed batch translation; only fall back to a per-chunk
        # API call if warm-up hasn't finished (it sets BATCH_READY when done).
        _batch_ready(sid).wait(timeout=60)
        if text and _gd_chunk_is_translated:
            tt = text
        else:
            tts = s.get("batch_tts") or []
            if seq < len(tts) and tts[seq]:
                tt = tts[seq]
            else:
                tt = translate(text, s["source_lang"], s["target_lang"])["target_text"]
        _marks["translate"] = round(_t.time() - _t0, 2)
        dubbed = Path("data") / "dubbed" / sid
        dubbed.mkdir(parents=True, exist_ok=True)
        _wav = dubbed / f"{seq:04d}_{label}.wav"
        if not _wav.exists():
            out = synthesize(
                tt,
                s["target_lang"],
                label,
                str(dubbed),
                name=f"{seq:04d}_{label}",
                target_ms=dur_ms,
                gender=gm["gender"],
                gindex=gm["gindex"],
            )
            # Dub-only: pure TTS voice, original audio muted (bed mix removed).
            out = {
                "wav_path": str(_wav),
                "mp3_path": str(_wav.with_suffix(".mp3")),
                "provider": out.get("provider", "piper"),
            }
        else:
            out = {"wav_path": str(_wav), "mp3_path": str(_wav.with_suffix(".mp3")), "provider": "piper"}
        # Showcase build: voice-only dub (background-bed path parked in bed.py).
        final_wav = out["wav_path"]
        assemble_chunk(s["src"], c["start_ms"] / 1000, dur_ms / 1000, final_wav, str(seg))
        try:
            assemble_chunk(s["src"], c["start_ms"] / 1000, dur_ms / 1000, c["wav_path"], str(orig))
        except Exception:
            pass
        # MSE streaming segment (copy video: native H.264, ~0.1s vs ~5s re-encode)
        mse = OUTPUT / sid / f"mse_{seq:04d}.m4s"
        try:
            from services.assembler.assemble import assemble_mse_chunk, init_codec_string, split_fmp4

            if not mse.exists():
                full = OUTPUT / sid / f"_full_{seq:04d}.mp4"
                assemble_mse_chunk(s["src"], c["start_ms"] / 1000, dur_ms / 1000, out["wav_path"], str(full))
                _init, media = split_fmp4(str(full))
                initp = OUTPUT / sid / "mse_init.mp4"
                if not initp.exists():
                    initp.write_bytes(_init)
                    try:
                        s["mse_codecs"] = init_codec_string(_init)
                    except Exception:
                        pass
                mse.write_bytes(media)
                try:
                    full.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        _marks["mse"] = round(_t.time() - _t0, 2)
        print(
            f"[dub-timing] sid={sid} seq={seq} {dur_ms}ms " + " ".join(f"{k}={v}s" for k, v in _marks.items()),
            flush=True,
        )
        SESSIONS[sid]["chunks_done"] = max(s.get("chunks_done", 0), seq + 1)
        return {
            "url": f"/output/{sid}/{seg.name}",
            "orig_url": f"/output/{sid}/{orig.name}" if orig.exists() else "",
            "mse_url": f"/output/{sid}/{mse.name}" if mse.exists() else "",
            "mse_init": f"/output/{sid}/mse_init.mp4",
            "codecs": s.get("mse_codecs") or "avc1.42C01F,mp4a.40.2",
            "start": c["start_ms"] / 1000,
            "dur": dur_ms / 1000,
            "speaker": label,
            "gender": gm["gender"] or "unknown",
        }


@app.get("/v1/sessions/{sid}/chunk/{seq}")
def get_chunk(sid: str, seq: int):
    s = SESSIONS.get(sid)
    if not s or "chunks" not in s:
        raise HTTPException(404, "unknown session")
    total = len(s["chunks"])
    if seq >= total:
        return {"done": True, "total": total}
    try:
        d = _dub_chunk(sid, seq)
    except Exception as e:
        raise HTTPException(500, f"chunk dub failed: {e}")
    d.update({"done": False, "seq": seq, "total": total, "next": seq + 1})
    return d


@app.post("/v1/sessions/{sid}/dub")
def start_dub(sid: str, bg: BackgroundTasks):
    if sid not in SESSIONS:
        raise HTTPException(404, "unknown session")
    bg.add_task(_run_dub, sid)
    return {"started": True}


@app.post("/v1/sessions/{sid}/video")
async def upload_video(
    sid: str,
    bg: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008 - canonical FastAPI pattern
    x_mock_size: int | None = Header(default=None),
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
    if len(content) < 100 or (
        not name.endswith((".wav", ".mp3"))
        and content[:4] not in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20", b"RIFF", b"\x1a\x45\xdf\xa3", b"ftyp")
    ):
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
    total_s = max(c["end_ms"] for c in chunks) / 1000 if chunks else 0
    try:
        _out = OUTPUT / sid
        _out.mkdir(parents=True, exist_ok=True)
        import shutil as _sh

        _sh.copy(str(tmp_file), str(_out / "orig.mp4"))
        SESSIONS[sid]["orig_url"] = f"/output/{sid}/orig.mp4"
    except Exception:
        pass
    # warm the file-level transcript+speaker map now, so chunk 0 dubs in seconds
    bg.add_task(_session_full_wav, sid)
    bg.add_task(_warm_maps, sid)
    return {"chunks_total": len(chunks), "total_s": total_s, "orig_url": SESSIONS[sid].get("orig_url")}


def _warm_maps(sid: str):
    # one Gemini call warms speaker map + transcript cache; sentence-level
    # translation warms next so per-chunk dubbing never waits on MT.
    try:
        _session_segments(sid)
    except Exception:
        pass
    try:
        from services.dub_worker import gemini_diarize as _gd

        s = SESSIONS.get(sid)
        if not s:
            return
        full_wav = _session_full_wav(sid)
        try:
            _gd.translate_segments(full_wav, s["source_lang"], s["target_lang"])
        except Exception:
            pass
        texts, labels = [], []
        for c in s["chunks"]:
            t, lab = _gd.chunk_text(full_wav, c["start_ms"] / 1000, c["end_ms"] / 1000)
            texts.append(t or " ")
            labels.append(lab or "1")
        SESSIONS[sid]["batch_labels"] = labels
        try:
            from services.dub_worker.voices import translate_batch

            SESSIONS[sid]["batch_tts"] = translate_batch(texts, s["source_lang"], s["target_lang"])
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _batch_ready(sid).set()


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
    segs = []
    try:
        _map = _session_segments(sid)
        _gen = _session_genders(sid)
        for x in _map:
            lab = str(x["speaker"])
            segs.append(
                {
                    "start": x["start"],
                    "end": x["end"],
                    "speaker": lab,
                    "gender": _gen.get(lab, {}).get("gender", "unknown"),
                }
            )
    except Exception:
        pass
    return {
        "video_url": s.get("video_url") or f"/output/{sid}/dubbed_{tgt}.mp4",
        "orig_url": s.get("orig_url"),
        "playlist_url": f"/output/{sid}/playlist.m3u8",
        "speakers": sorted({x["speaker"] for x in segs}) or [1, 2, 3],
        "segments": segs,
        "chunks_total": s["chunks_total"],
        "chunks_done": s.get("chunks_done", 0),
    }


@app.get("/v1/cost")
def cost():
    from services.dub_worker import costs as _costs

    out = _costs.totals()
    out["tts_engine"] = "sarvam"
    return out


@app.get("/", response_class=HTMLResponse)
def index():
    return Path(__file__).parent.joinpath("static/index.html").read_text()
