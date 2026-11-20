# Lingofy — video dubbing that keeps the voices straight

Upload a video, pick EN→HI or HI→EN, hit **Dub it**. Playback starts in seconds
with a speaker timeline; the dubbed track renders ahead of you while you watch.

Live demo: **https://lingofy-dun.vercel.app** (one-click **Bo-Mitra 🎬** sample,
no upload needed) → API on EC2.

## How it works

```
upload (mp4/wav)
  │  chunker: 20s wav slices (+ sub-second tails absorbed, no slivers)
  ▼
diarize ......... Gemini 3.5-transcribe, once per video, cached on disk by hash
transcribe ...... word timestamps sliced from the same cache per chunk
translate ....... ONE batched Gemini call for all chunks, cached by content hash
gender .......... wav2vec2 check per speaker (warmed at boot, not on stage)
synth ........... Sarvam Bulbul v4-flash per chunk (temp 0.2, retried 3x, then Piper)
                  each fitted to its exact slot, pure voice-only (no bed mix)
assemble ........ ffmpeg mux: dubbed_hi.mp4 + per-chunk mse_*.m4s segments
  ▼
playback ........ MSE streaming (3 chunks ahead), seeking anywhere, dubbed/original toggle
```

Same speaker keeps the same Sarvam persona in every chunk (gender-matched,
deterministic per speaker). A failed Sarvam call retries with backoff — Piper
only voices a chunk after 3 failed attempts, and the fallback is logged loudly.

## Run it

**Local (same images as prod):**
```bash
docker compose -f deploy/dev/docker-compose.yml up --build
# UI: http://localhost:8080
```

**Prod:** Vercel frontend (`services/gateway/static`, auto-deploys on push to
`main`) proxying an EC2 gateway (`deploy/dev/docker-compose.aws.yml` adds the
key mounts). CI (`ci.yaml`: ruff → pytest → build → trivy) plus
`deploy-aws.yml` (SSH: pull → rebuild → health-check) run on every push.

## Keys (never in git)

| File | What |
|---|---|
| `~/.config/lingofy/sarvam.json` | `{"keys": [...]}` — Bulbul TTS, rotated on 429/quota |
| Gemini pool (`GEMINI_KEYS_FILE`, default `.../SVAR/credentials/gemini-api-keys.json`) | diarize + translate + subs only — never TTS |

Cost ledger: `data/cost_ledger.jsonl`, live at `/v1/cost` (`sarvam_inr_est` ≈ ₹1/1k chars).

## Endpoints that matter

- `POST /v1/sessions` → `{session_id}` · `POST /v1/sessions/{sid}/video` (multipart)
- `POST /v1/sessions/{sid}/dub` → background dub · `GET /v1/sessions/{sid}/status`
- `GET /v1/sessions/{sid}/chunk/{n}` → `{mse_url, mse_init, codecs, ...}` (MSE)
- `GET /v1/sessions/{sid}/manifest` → speakers, timeline, dubbed + original urls
- `GET /samples/bo-mitra.mp4` → built-in 2-min demo clip
- `GET /metrics` → Prometheus (sessions, chunks, Sarvam spend)

## Honest scope notes

- Queues are local files (`data/queue/`); the SQS/KEDA Terraform exists but the
  app data-plane switch is future work. One node, sessions in memory.
- Diarize/translate/words caches are content-hashed on disk — re-uploads and
  stage re-runs cost zero Gemini calls. Fresh boot warms the gender model in
  the background (~2 min on 2 vCPU) so the first dub doesn't pay for it.
- 20s chunks: fewer prosody seams than 8s, still quick first paint. Cut points
  need keyframes underneath — the sample is encoded with forced keyframes
  every 20s; uploads cut on the nearest keyframe instead.
-encoders: the bundled ffmpeg has no libx264 — trims use `libopenh264`
  (`-g 48 -force_key_frames …`) or mpeg4 (browsers won't play it via MSE).
