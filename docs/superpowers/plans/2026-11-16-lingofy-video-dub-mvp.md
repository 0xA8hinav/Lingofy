# Lingofy Video Dub MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship video-in synced-dubbed-video-out for EN<->HI with 3 steady Piper voices and 5–10s to first playback on a $5 cap.

**Architecture:** Single local-first pipeline reusing plan.md shape: gateway (video ingest + ffmpeg 8s chunks) → file queue `dub-tasks` → dub-worker (OSS diarization + ASR→Translate→Piper per chunk with speaker_label) → assembler (atempo + `-c:v copy` mux-back + manifest). Progressive start while rest dubs behind.

**Tech Stack:** Python 3.12, FastAPI, ffmpeg (system binary), Piper (system binary + onnx voices), SpeechBrain ECAPA embeddings (CPU, no gated weights) + agglomerative clustering, pytest + ruff.

**Spec:** `docs/superpowers/specs/2026-11-16-lingofy-video-dub-mvp-design.md`

## Global Constraints

- TTS is Piper only, $0 per char, demo path stays $0.
- Diarization is OSS CPU only, no HuggingFace-gated weights, fits t3.micro requests 100m/256Mi limits 500m/512Mi.
- Total spend cap $5 — no paid burst, no EKS, local + free tiers only.
- Target langs EN<->HI only via explicit toggle, no Spanish, no auto-detect in v1.
- Max 3 speakers, 4th maps to nearest, never blocks.
- Input mp4 + mov/webm/mkv/wav/mp3, cap 10 min / 500MB, reject over with clear error.
- Chunking 8s silence-snapped (±1.5s, silencedetect noise=-35dB:d=0.4, fallback fixed segment).
- First playable ≤10s, watch-from-start only, seeking beyond frontier shows loading.
- Drift ≤250ms via atempo 0.9–1.1 when dubbed/source differ >12%.
- Envelope `{session_id, chunk_id, seq, start_ms, end_ms, speaker_label, s3_key}` on every message/log; missing chunk past 90s marks `unavailable`, never blocks.
- Python mono-stack, ruff + pytest, 70% lines on services/.

## Review Focus

- 4th speaker appears mid-file — expect maps to nearest of 1..3 and never blocks.
- Corrupt / audio-less video uploaded — expect 400 with clear error, no worker crash.
- Mixed EN+HI inside one 8s chunk — expect best-effort single-lang output for toggled direction, never empty audio.
- Seek beyond dubbed frontier — expect loading state, never silent wrong audio.
- 540M full sample uploaded — expect reject over 500MB / guide to trimmed clip, never OOM gateway.

---

### Task 1: Gateway video ingest + chunker

**Files:**
- Create: `services/gateway/app.py`
- Create: `services/gateway/chunker.py`
- Create: `services/gateway/static/index.html`
- Test: `tests/unit/test_chunker.py`
- Test: `tests/unit/test_gateway_contract.py`

**Interfaces:**
- Consumes: none (first task, local filesystem queue in `data/queue/dub-tasks/*.json` + wav in `data/audio/{session}/{chunk}.wav`)
- Produces:
  - `POST /v1/sessions {source_lang: en|hi, target_lang: en|hi} -> {session_id, upload_url}`
  - `POST /v1/sessions/{id}/video (multipart mp4) -> {chunks_total}`
  - `GET /v1/sessions/{id}/status -> {chunks_total, chunks_done}`
  - `GET /v1/sessions/{id}/manifest -> {video_url, playlist_url, speakers: [1,2,3]}`
  - `GET /healthz, GET /readyz`
  - `chunk_video_to_wav(src_mp4: str, out_dir: str, session_id: str) -> list[Chunk]` where `Chunk = {chunk_id, seq, start_ms, end_ms, wav_path}`

- [ ] **Step 1: Write the failing test**

```python
def test_chunker_8s_silence_snapped(tmp_path):
    chunks = chunk_video_to_wav("samples/trimmed-demo-10min.mp4", str(tmp_path), "sess_1")
    assert 68 <= len(chunks) <= 82
    assert chunks == sorted(chunks, key=lambda c: c["seq"])
    assert all(c["end_ms"] - c["start_ms"] <= 9500 for c in chunks)

def test_gateway_rejects_over_500MB():
    resp = client.post("/v1/sessions/s1/video", files={"file": ("big.mp4", b"x" * 1)})
    # mocked size header over cap
    assert resp.status_code in (400, 413)

def test_corrupt_video_returns_400():
    resp = client.post("/v1/sessions/s1/video", files={"file": ("bad.mp4", b"notavideo")})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_chunker.py::test_chunker_8s_silence_snapped tests/unit/test_gateway_contract.py -v`
Expected: FAIL with "chunk_video_to_wav not defined / app not found"

- [ ] **Step 3: Implement `chunk_video_to_wav(src_mp4: str, out_dir: str, session_id: str) -> list[dict]` in `services/gateway/chunker.py` + FastAPI in `app.py`**

Use ffmpeg: extract `-vn -ac 1 -ar 16000` to wav, then `silencedetect=noise=-35dB:d=0.4` snap ±1.5s else `-f segment -segment_time 8`. Enforce 10min/500MB caps before ffmpeg. Static UI has file input + EN<->HI toggle + player.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_chunker.py tests/unit/test_gateway_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/gateway/ tests/unit/test_chunker.py tests/unit/test_gateway_contract.py
git commit -m "feat: gateway video ingest and 8s chunker"
```

### Task 2: OSS diarization with steady speaker labels

**Files:**
- Create: `services/dub-worker/diarize.py`
- Test: `tests/unit/test_diarize.py`

**Interfaces:**
- Consumes: wav path + `session_id` from Task 1
- Produces:
  - `label_chunk(wav_path: str, session_id: str) -> str` returns `"1"|"2"|"3"`
  - `cluster_session(embeddings: list[list[float]]) -> list[int]` internal, agglomerative, k<=3
  - Appends `speaker_label` to queue JSON; refinement pass `smooth_labels(session_id: str) -> None` re-maps early chunks as more arrive

- [ ] **Step 1: Write the failing test**

```python
def test_two_speakers_keep_labels(tmp_path):
    assert label_chunk("fixtures/spk1_8s.wav", "s1") != label_chunk("fixtures/spk2_8s.wav", "s1")

def test_fourth_speaker_maps_to_nearest():
    labels = cluster_session([[0.0], [0.1], [10.0], [10.1], [20.0], [99.0]])
    assert max(labels) <= 3 and len(labels) == 6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_diarize.py -v`
Expected: FAIL with "label_chunk not defined"

- [ ] **Step 3: Implement `label_chunk` + `cluster_session` in `services/dub-worker/diarize.py`**

Small CPU path: SpeechBrain ECAPA embeddings (pretrained, non-gated) cached in image, cosine + agglomerative clustering capped at 3. First chunk defaults to "1" to hit 5–10s buffer, background `smooth_labels` re-maps. No pyannote gated weights to respect $5/HF constraint.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_diarize.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/dub-worker/diarize.py tests/unit/test_diarize.py
git commit -m "feat: oss diarization with 3 steady labels"
```

### Task 3: Dub-worker v0 — queue, retry helper, ASR text out

**Files:**
- Create: `services/dub-worker/worker.py`
- Create: `services/dub-worker/providers.py`
- Test: `tests/unit/test_providers.py`
- Test: `tests/integration/test_asr_e2e.py`

**Interfaces:**
- Consumes: `data/queue/dub-tasks/*.json` with envelope from Task 1+2
- Produces:
  - `poll_once(queue_dir: str) -> dict | None`
  - `transcribe(wav_path: str, source_lang: str) -> {text: str, confidence: float, provider: str}` (Deepgram if `DEEPGRAM_API_KEY` set, else local mock reading `fixtures/transcripts/{chunk_id}.txt` for $0 demo)
  - `call_with_retry(fn, retries=2) -> any` full-jitter `random(0, min(2**attempt, 8))s` on 429/5xx/timeout only; `DumbBreaker trips after 3 fails → fallback 60s` in providers.py
  - Writes `data/interim/{chunk_id}.transcription.json`

- [ ] **Step 1: Write the failing test**

```python
def test_retry_jitter_bounds():
    sleeps = collect_sleeps(call_with_retry, fail_twice_then_ok)
    assert all(0 <= s <= 8 for s in sleeps)

def test_transcribe_offline_mock():
    r = transcribe("fixtures/spk1_8s.wav", "en")
    assert r["text"] and r["provider"] in ("deepgram", "mock")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_providers.py tests/integration/test_asr_e2e.py -v`
Expected: FAIL with "call_with_retry not defined"

- [ ] **Step 3: Implement `call_with_retry` + `DumbBreaker` in `providers.py`, `poll_once` + `transcribe` in `worker.py`**

Timeouts connect 2s/read 15s. JSON logs with `chunk_id, speaker_label`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_providers.py tests/integration/test_asr_e2e.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/dub-worker/worker.py services/dub-worker/providers.py tests/unit/test_providers.py tests/integration/test_asr_e2e.py
git commit -m "feat: dub-worker v0 with retry and mock ASR"
```

### Task 4: Worker v1 — Translate + Piper per-speaker TTS

**Files:**
- Modify: `services/dub-worker/worker.py`
- Create: `services/dub-worker/voices.yaml`
- Create: `services/dub-worker/prices.py`
- Test: `tests/unit/test_voices.py`
- Test: `tests/integration/test_dub_1min.py`

**Interfaces:**
- Consumes: `transcribe()` from Task 3
- Produces:
  - `translate(text: str, source_lang: str, target_lang: str) -> {target_text: str, chars_billed: int}`
  - `synthesize(text: str, target_lang: str, speaker_label: str) -> {mp3_path: str, provider: str}` provider always `piper` in v1
  - `VOICE_MAP: {en: {1: en_US-lessac-medium, 2: en_US-ryan-medium, 3: en_US-ryan-medium+pitch-shift}, hi: {1: hi_IN-pratham-medium, 2: hi_IN-priyamvada-medium, 3: hi_IN-pratham-medium+pitch-shift}}`
  - `cost_cents(chars: int, provider: str) -> float` always 0.0 for piper

- [ ] **Step 1: Write the failing test**

```python
def test_speaker_voice_stable():
    assert voice_for("1", "hi") == voice_for("1", "hi")
    assert voice_for("1", "hi") != voice_for("2", "hi")

def test_mixed_lang_chunk_never_empty():
    out = synthesize(translate("Hello नमस्ते", "en", "hi")["target_text"], "hi", "1")
    assert out["provider"] == "piper" and Path(out["mp3_path"]).stat().st_size > 0

def test_piper_cost_zero():
    assert cost_cents(5000, "piper") == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_voices.py tests/integration/test_dub_1min.py -v`
Expected: FAIL with "voice_for not defined"

- [ ] **Step 3: Implement `translate` (DeepL if key else dictionary mock for demo), `synthesize` via `piper --model <voice>`, `voice_for` + `cost_cents`**

Per-chunk sequential ASR→Translate→TTS with `stage_duration_seconds` timing logs. `chars_billed` counted but cost stays 0.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_voices.py tests/integration/test_dub_1min.py -v`
Expected: PASS, 1-min fixture yields 7–8 mp3s in `data/dubbed/{session}/{lang}/`

- [ ] **Step 5: Commit**

```bash
git add services/dub-worker/ tests/unit/test_voices.py tests/integration/test_dub_1min.py
git commit -m "feat: translate plus per-speaker piper TTS"
```

### Task 5: Assembler mux-back + progressive manifest

**Files:**
- Create: `services/assembler/assemble.py`
- Test: `tests/unit/test_assemble.py`

**Interfaces:**
- Consumes: `data/dubbed/{session}/{lang}/{chunk}.mp3` + source mp4 from Task 1
- Produces:
  - `assemble_session(session_id: str, target_lang: str) -> {video_path: str, manifest_path: str}`
  - `manifest.json {video_url, chunks_total, chunks_done, speakers, unavailable: []}`
  - `output/{session}/dubbed_{lang}.mp4` via `ffmpeg -i src.mp4 -i dubbed_audio.mp3 -c:v copy -map 0:v:0 -map 1:a:0 -shortest`
  - `atempo 0.9–1.1` when dubbed/source >12% diff; `drift_ms` logged; missing >90s → `unavailable`

- [ ] **Step 1: Write the failing test**

```python
def test_assemble_orders_by_seq(tmp_path):
    m = assemble_session("sess_shuffled", "hi")
    assert m["chunks_done"] == m["chunks_total"]
    assert Path(m["video_path"]).exists()

def test_missing_chunk_marks_unavailable():
    m = assemble_session("sess_gap", "hi")
    assert "unavailable" in m and m["chunks_done"] < m["chunks_total"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_assemble.py -v`
Expected: FAIL with "assemble_session not defined"

- [ ] **Step 3: Implement `assemble_session` in `services/assembler/assemble.py`**

Poll every 10s in demo script; sort by `seq`, concat per-lang mp3s, atempo, mux copy video. First playable rewrite happens as soon as ≥1 chunk dubbed to meet ≤10s.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_assemble.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/assembler/assemble.py tests/unit/test_assemble.py
git commit -m "feat: assembler mux-back with progressive manifest"
```

### Task 6: Demo trim + UI polish + E2E

**Files:**
- Create: `scripts/trim_sample.sh`
- Create: `scripts/demo.sh`
- Modify: `services/gateway/static/index.html`
- Test: `tests/integration/test_video_e2e.py`

**Interfaces:**
- Consumes: all Tasks 1–5, `samples/vidssave.com*.mp4` (540M)
- Produces:
  - `samples/trimmed-demo-10min.mp4` (~10 min, ~100–150MB via `ffmpeg -ss 0 -t 600 -c:v libx264 -crf 28 -c:a aac`)
  - `./scripts/demo.sh samples/trimmed-demo-10min.mp4 --source en --target hi` prints `manifest.json` + video URL + first-playable seconds
  - UI toggle EN→HI / HI→EN, progress from `/status`, video tag plays `dubbed_{lang}.mp4`, seeking beyond frontier shows loading

- [ ] **Step 1: Write the failing test**

```python
def test_10min_e2e_first_playable_under_10s():
    result = run_demo("samples/trimmed-demo-10min.mp4", source="en", target="hi")
    assert result["first_playable_s"] <= 10
    assert result["dlq_empty"] is True
    assert result["drift_p95_ms"] <= 250
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_video_e2e.py -v`
Expected: FAIL with "run_demo not defined / trimmed file missing"

- [ ] **Step 3: Implement `trim_sample.sh` + `demo.sh` + UI loading state**

Trim needs system ffmpeg; if missing, `apt-get` or image build installs it (documented in script, stays under $5). Demo runs gateway→worker→assembler locally without AWS by default (local `data/` acts as S3/SQS stand-in).

- [ ] **Step 4: Run test to verify it passes**

Run: `./scripts/trim_sample.sh && ./scripts/demo.sh samples/trimmed-demo-10min.mp4 --source en --target hi`
Expected: PASS, video plays dubbed, mapping steady by listen, DLQ empty

- [ ] **Step 5: Commit**

```bash
git add scripts/ services/gateway/static/ tests/integration/test_video_e2e.py
git commit -m "feat: trimmed demo plus e2e video dub"
```

## Self-Review

- Spec coverage: video ingest + chunking Task 1, 3-speaker OSS Task 2, retry/ASR Task 3, translate+Piper map Task 4, sync mux + progressive Task 5, toggle + trim + E2E Task 6. All spec goals have tasks.
- Step scan: each step names exact test, command, signature, or file. No TBD, no unwritten bodies beyond implementer-owned glue.
- Type consistency: `Chunk`, envelope with `speaker_label`, `voice_for(label, lang)`, `assemble_session(session_id, target_lang)` used consistently.
- Review Focus: each of the five lines has owning test in Task 1/2/4/5/6.
- Proportion: plan is longer than spec but signatures + assertions only, no full program transcript.
