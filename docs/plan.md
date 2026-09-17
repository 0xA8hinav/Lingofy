# Lingofy Implementation Plan

> Track progress with the checkboxes (`- [ ]`) under each task. Work tasks in order — each one assumes the previous ones are done.

**Goal:** Ship a cloud-native multilingual dubbing pipeline that holds p95 end-to-end latency under 3 seconds across 5 stages, 3 paid AI APIs, and a KEDA-scaled worker fleet — with live SLO, chaos, and cost demos.

**Architecture:** Chunk-parallel pipeline (8s ffmpeg chunks) → SQS stage queues → KEDA workers → AI Gateway (retry / circuit-breaker / fallback / metering) → Assembler (HLS output). See `docs/architecture.md` and `README.md` §2.

**Tech Stack:** Python 3.12 · FastAPI · ffmpeg · boto3 · LiteLLM-style gateway · k3s/k3d · KEDA · Helm · ArgoCD · GitHub Actions · Trivy · cosign · OpenTelemetry · Prometheus · Grafana · Alertmanager · k6 · AWS (SQS, S3, ECR, IAM)

**Spec:** `docs/TECHNICAL_REFERENCE.md` is the single source of truth. If this plan needs content not in the Tech Ref, fix the Tech Ref first — do not let the two drift.

## Global Constraints

- p95 end-to-end chunk latency < 3.0s (`lingofy_session_e2e_latency_seconds`), alert if breached for 5 min.
- Stage success rate ≥ 99.5% per stage; timeline drift ≤ 250 ms; gateway 99% monthly.
- Semester spend ≤ $50 (+$50 buffer); AWS Budgets alerts at $40/$70.
- Python mono-stack v1 (one base `python:3.12-slim`, `ruff + mypy + pytest`); Go only as optional bounded `ws-relay` in week 11.
- Workers hold no provider credentials; keys live in AI Gateway only (K8s Secrets from OIDC → SSM, never in git).
- Histogram buckets `0.05,0.1,0.2,0.4,0.8,1.6,3.2,6.4` so p95 lands inside a real bucket.
- `minReplicaCount: 1` (no scale-to-zero in demo); `pytest` ≥ 80% lines on `services/`.

---

## How to read this plan

Each task below is self-contained: start with **Outcome** (what exists when you finish), then **Why** (what problem it solves), then **Build** (exact files and what goes in them), then **Verify** (the command to run and what success looks like). Tasks run in order — Task N assumes Tasks 1..N-1 are done. Owners: A = Aarav (queues, KEDA, Helm, ArgoCD, CI/CD, IaC, cost), B = Abhinav (gateway, workers, AI gateway, tracing), C = Anshuman (UI, k6, chaos, dashboards).

## File map (where everything lives)

- `services/gateway/` — FastAPI REST + WebSocket ingest, session lifecycle, `/healthz` `/readyz`
- `services/slicer/` — ffmpeg chunking (silence-snapped 8s), S3 writes, manifest records
- `services/worker-asr/` `worker-translate/` `worker-tts/` — SQS consumers that call providers via AI Gateway
- `services/ai-gateway/` — single egress for all AI calls: retries, circuit breaker, fallback, rate limits, cost metering (`prices.yaml`)
- `services/assembler/` — reorders chunks, time-stretches with `atempo`, emits HLS playlists + `manifest.json`
- `deploy/terraform/` — AWS-side only: S3 buckets, 6 SQS queues + DLQ, ECR repos, IAM roles
- `deploy/helm/lingofy/` — umbrella chart + subchart per service, `values-dev.yaml` / `values-demo.yaml`
- `deploy/keda/` — `TriggerAuthentication` + `ScaledObject` per worker
- `deploy/observability/` — Prometheus recording/alert rules, Grafana dashboard JSON, Alertmanager routes + remediation Jobs
- `deploy/argocd/` — ArgoCD `Application` manifests (repo is source of truth, cluster syncs from it)
- `.github/workflows/` — `ci.yaml` (PR checks), `cd.yaml` (deploy + rollback), `chaos-nightly.yaml` (load + fault injection)
- `tests/unit|integration|load|chaos/` + `scripts/demo.sh load.sh destroy-demo-week.sh` + `samples/`

---

## Timeline (12 weeks, Mon–Sun)

| Wk  | Dates           | Milestone                                                          | Owner  | Depends on |
| -----| -----------------| --------------------------------------------------------------------| --------| ------------|
| 1   | Sep 21–27, 2026 | Repo bootstrap, ADRs, IaC skeleton                                 | A      | —          |
| 2   | Sep 28–Oct 4    | Gateway + session store                                            | B      | T1         |
| 3   | Oct 5–11        | Slicer (chunker)                                                   | B      | T2         |
| 4   | Oct 12–18       | Queues + ASR worker + AI Gateway v0                                | A+B    | T1–T3      |
| 5   | Oct 19–25       | Translate + TTS workers (ElevenLabs + Piper fallback)              | B      | T4         |
| 6   | Oct 26–Nov 1    | Assembler + HLS output                                             | A      | T5         |
| 7   | Nov 2–8         | KEDA autoscaling + ArgoCD GitOps deploys                           | A      | T1–T6      |
| 8   | Nov 9–15        | Observability (traces, metrics, dashboards, alerts)                | C+B    | T1–T7      |
| 9   | Nov 16–22       | Resilience (circuit breaker, DLQ redrive, chaos, auto-remediation) | A      | T4–T8      |
| 10  | Nov 23–29       | Cost metering + image signing                                      | A+C    | T4–T5, T8  |
| 11  | Nov 30–Dec 6    | Web UI + full load/chaos pass (+ optional Go relay)                | C (+B) | T1–T10     |
| 12  | Dec 7–13        | Freeze, rehearsal, demo                                            | all    | T1–T11     |

---

### Task 1: Repo bootstrap, ADRs, IaC skeleton (Wk 1, Sep 21–27 — A)

**Outcome:** Anyone cloning the repo gets the full folder layout, a `terraform plan` that previews all AWS resources without creating them, and a green CI run on an empty PR.

**Why:** Every later task assumes the folders, Terraform modules, and CI checks already exist. Doing this first prevents mid-project restructuring and forces the big cost decisions (k3s vs EKS, SQS vs Kafka, 8s chunks, Piper fallback) to be written down as ADRs before code is written.

**Build:**
- Create docs skeleton: `docs/TECHNICAL_REFERENCE.md`, `docs/architecture.md`, `docs/plan.md` (this file), `docs/runbook.md` (headings only for now).
- Create `deploy/terraform/*.tf`: one S3 bucket for audio, one for output, 6 SQS queues (`ingest/asr/translate/tts/assemble` + shared `lingofy-dlq` with redrive policy), ECR repos per service, one least-privilege IAM role per worker. Do not `apply` yet — `plan` only.
- Create `.github/workflows/ci.yaml` skeleton: `ruff lint` + `mypy` → `pytest` → `docker build` per changed service → `trivy image --severity CRITICAL,HIGH` → `kind` + `helm template | kubectl apply --dry-run=server` + 1-min smoke clip.

**Verify:**
- [ ] `terraform -chdir=deploy/terraform plan` shows SQS/S3/ECR changes, zero errors.
- [ ] Open an empty PR → `ci.yaml` goes green.
- [ ] ADRs 1–7 recorded in `README.md` §6 and Tech Ref §15.

Status: `pending`.

### Task 2: Gateway + session store (Wk 2, Sep 28–Oct 4 — B)

**Outcome:** Clients can create a dubbing session, get an upload URL (or open a live-audio WebSocket), and poll session status. The OpenAPI spec is committed so workers and UI can code against it.

**Why:** The gateway is the only entry point. The slicer, workers, assembler, and UI all depend on its session IDs, S3 keys, and message envelope. Freezing this contract early unblocks Tasks 3–6 and 11 in parallel.

**Build:**
- Create `services/gateway/app.py` (FastAPI + uvicorn): `POST /v1/sessions` (takes `{source_lang, target_langs[], ttl_hours}`, returns `{session_id, upload_url}`), `POST /v1/sessions/{id}/audio` (multipart file upload), `WS /v1/sessions/{id}/stream` (binary audio frames, skeleton only for now), `GET /v1/sessions/{id}/status`, `GET /v1/sessions/{id}/manifest`, `GET /healthz` (liveness), `GET /readyz` (checks SQS + S3 reachability).
- Session store: SQLite file on a PVC for local dev; same interface backed by DynamoDB or Postgres on the demo node. Store `{session_id, source_lang, target_langs, chunks_total, chunks_done, ttl}`. Add a TTL reaper (delete after `ttl_hours`).
- Keep the gateway stateless otherwise — all audio bytes go to S3, all progress lives in the store.

**Verify:**
- [ ] `pytest tests/unit/test_gateway_contract.py -v` passes (creates session → uploads 10s clip → status shows `chunks_total >= 0`).
- [ ] `openapi.json` is generated and committed; `GET /readyz` returns 200 against localstack.
- [ ] Contract test fails if any endpoint path or field name drifts.

Status: `pending`. Depends on: T1.

### Task 3: Slicer — turn one audio file into 8s chunks (Wk 3, Oct 5–11 — B)

**Outcome:** Given a 5-minute MP3/WAV, the slicer produces ~38 WAV chunks in S3 plus one manifest record per chunk (`session_id, chunk_id, seq, start_ms, end_ms`), with boundaries snapped to silence so words are not cut mid-sentence.

**Why:** Chunks are the unit of parallelism for the whole system. If chunking is wrong (too many tiny calls, or cuts in the middle of speech), ASR accuracy, API cost, and end-to-end latency all suffer. This task locks the 8-second contract every downstream worker relies on.

**Build:**
- Create `services/slicer/chunker.py` + `Dockerfile` (base `ffmpeg:6-alpine`, Python wrapper around the binary).
- Smart split: run `ffmpeg -i in -af silencedetect=noise=-35dB:d=0.4 -f null -`, parse silence timestamps, snap each 8s boundary to the nearest silence within ±1.5s. If no silence is found, fall back to fixed `ffmpeg -i input.wav -vn -ac 1 -ar 16000 -f segment -segment_time 8 -reset_timestamps 1 chunk_%04d.wav` (16 kHz mono, ASR-friendly).
- Per chunk: write `s3://audio/{session_id}/{chunk_id}.wav`, append manifest record via conditional PUT (seq-locked, last-write-wins), enqueue one `ChunkCreated` message (envelope from Tech Ref §2.1).

**Verify:**
- [ ] `pytest tests/unit/test_chunker.py -v` passes: `samples/sample-en-5min.mp3` → 38±2 chunks, every boundary within 1.5s of a silence point (or flagged as fixed-split).
- [ ] Re-running on the same session is idempotent (same `seq` overwrites, no duplicates).

Status: `pending`. Depends on: T2 (needs session IDs and S3 bucket names).

### Task 4: Queues + ASR worker + AI Gateway v0 (Wk 4, Oct 12–18 — A+B)

**Outcome:** A chunk placed on `asr-tasks` comes back as transcribed text on `translate-tasks` in under a second (p50), with retries and tracing, without the worker ever holding a provider API key.

**Why:** This is the first full crossing of the pipeline (queue → worker → AI Gateway → external API → next queue). It proves the envelope schema, the SQS settings (visibility timeouts, DLQ), and the gateway retry/timeout behavior that Tasks 5–6 will reuse unchanged.

**Build:**
- Terraform: `deploy/terraform/sqs.tf` — create the 6 queues for real with the Tech Ref §2.3 settings (`asr-tasks`: visibility 60s, maxReceive 3 → shared DLQ, 4-day retention). Local dev uses localstack with identical names.
- Create `services/ai-gateway/proxy.py` v0: forwards to Deepgram, retries up to 3 times on 429/5xx/timeout with full-jitter backoff `sleep = random(0, min(2^attempt, 8))s`, honors `Retry-After`, timeouts connect 2s / read 15s for ASR. Must use Deepgram's low-latency streaming endpoint (the pre-recorded REST endpoint can exceed 20s and breaks the 3s budget).
- Create `services/worker-asr/worker.py`: long-polls `asr-tasks` (10s, 1 message at a time), downloads the WAV from S3, calls Deepgram *through* the AI Gateway, publishes `TranscriptionDone {text, confidence, provider, asr_ms}` to `translate-tasks` with the same `trace_id`/`chunk_id`.
- Propagate `chunk_id` as the OTel trace correlation key from the start.

**Verify:**
- [ ] `pytest tests/integration/test_asr_e2e.py -v` passes: 1 synthetic chunk → transcription appears on `translate-tasks` in < 1s p50, trace shows `slicer.chunk → asr.call` spans.
- [ ] Killing the worker mid-call redelivers the message after visibility timeout (no loss, no duplicate transcription downstream).
- [ ] Worker container has zero provider env vars (`env | grep -i deepgram` is empty outside the gateway).

Status: `pending`. Depends on: T1 (Terraform), T2 (envelope), T3 (chunks).

### Task 5: Translate + TTS workers with fallback (Wk 5, Oct 19–25 — B)

**Outcome:** Transcribed text fans out into one message per target language, and each language comes back as playable audio — from ElevenLabs when healthy, from self-hosted Piper when ElevenLabs fails or quota runs out — with zero dropped chunks either way.

**Why:** This is where the "dub" actually happens and where paid-API risk lives (DeepL character quota, ElevenLabs character quota). The fallback path (Piper sidecar) is the core resilience story for the provider-outage demo, so it must be built alongside the happy path, not bolted on later.

**Build:**
- Create `services/worker-translate/worker.py`: consumes `translate-tasks`, calls DeepL REST via AI Gateway, emits one `TranslationDone {source_text, target_text, chars_billed}` message per target language (default `es,hi`) onto `tts-tasks`. Report `chars_billed` in the gateway response header for cost metering.
- Create `services/worker-tts/worker.py` + Piper sidecar in the same pod (talks over unix socket): primary ElevenLabs `eleven_turbo_v2` via gateway; fallback to Piper voices pinned per language (`es_ES-mls_1024-low`, `hi_IN-pratham-medium`) whenever the ElevenLabs circuit is open or the character quota is exhausted. Write `s3://audio/{session_id}/{lang}/{chunk_id}.mp3`, emit `SynthesisDone {audio_s3_key, duration_ms, provider, chars_billed}` to `assemble-tasks`.

**Verify:**
- [ ] `pytest tests/integration/test_dub_1min.py -v` passes: 1-min EN clip → ES + HI audio chunks in S3, all playable.
- [ ] Forced-failure test passes: stub ElevenLabs to return 500s → all chunks still complete with `provider=piper` and the fallback counter increments.
- [ ] `chars_billed` per chunk matches DeepL/ElevenLabs dashboard within rounding.

Status: `pending`. Depends on: T4 (gateway pattern + `translate-tasks` input).

### Task 6: Assembler + HLS output (Wk 6, Oct 26–Nov 1 — A)

**Outcome:** Per-language MP3 chunks become a streamable HLS playlist plus a session manifest, with dubbed audio kept within 250 ms of the original timeline. A missing chunk never blocks the whole session — it is marked `unavailable` after 90s.

**Why:** Workers produce out-of-order, variably-sized audio (translated sentences are longer/shorter than the source). Without reordering, time-stretching, and a playlist format, the client gets a bag of files instead of a watchable dubbed track.

**Build:**
- Create `services/assembler/assemble.py`: consume `assemble-tasks`, group by `(session_id, lang)`, sort by `seq`, verify contiguity (`end_ms[i] ≈ start_ms[i+1]`). If a dubbed chunk differs from its source chunk by > 12%, run `ffmpeg -af atempo=0.9..1.1` (chain filters for larger ratios) to fit without pitch shift. Emit `s3://output/{session_id}/{lang}/playlist.m3u8` (2s segments) + `{session_id}/manifest.json` (`{lang, playlist_url, drift_ms, unavailable[]}`).
- Session SLA: if a chunk is still missing 90s after the session started, mark that `seq` as `unavailable` in the manifest and finalize without it.

**Verify:**
- [ ] `pytest tests/unit/test_assembler_drift.py -v` passes: 5-min sample → `lingofy_drift_ms` p95 ≤ 250 ms per language.
- [ ] Out-of-order delivery test: shuffle 10 chunks → playlist order still matches `seq`.
- [ ] Missing-chunk test: withhold 1 chunk for 90s → manifest finalizes with 1 `unavailable` entry, playlist still plays.

Status: `pending`. Depends on: T5 (needs real TTS output).

### Task 7: KEDA autoscaling + ArgoCD deploys (Wk 7, Nov 2–8 — A)

**Outcome:** A burst of sessions automatically grows workers from 1 to ~12 replicas on queue backlog and shrinks back when drained — and every deploy happens by syncing the repo to the cluster, not by running `helm upgrade` by hand.

**Why:** Autoscaling is the "elastic pipeline" claim; GitOps is what makes deploys repeatable for the demo (one sync command, auditable, rollback-able). Both must land before observability (Task 8) so the scaling event can be graphed.

**Build:**
- Create `deploy/keda/*.yaml`: one `TriggerAuthentication` (IRSA on EKS, accessKey/secretKey Secret on k3s) + one `ScaledObject` per worker. Settings: `queueLength: 5` (1 replica per 5 pending messages), `minReplicaCount: 1`, `maxReplicaCount: 12` (TTS capped at 8 — Piper sidecar is CPU-bound), `cooldownPeriod: 120`, `pollingInterval: 10`. Requests/limits per worker e.g. `150m/256Mi` request, `500m/512Mi` limit.
- Create `deploy/argocd/*.yaml`: one `Application` per environment watching `deploy/helm/lingofy` + values overlay (`values-dev.yaml` / `values-demo.yaml`). `kubectl apply -f deploy/argocd/` bootstraps; `argocd app sync lingofy` is the deploy. Keep `helm upgrade --install` documented as fallback.
- Create `tests/load/k6-burst.js` + `scripts/load.sh`: ramp to 50 concurrent sessions of 3-min audio.

**Verify:**
- [ ] `./scripts/load.sh --sessions 60` → Grafana shows replicas 1→12 and backlog returning to ~0 within 60s of burst end; screenshot saved to `docs/`.
- [ ] Fresh `k3d` cluster + `argocd app sync` → all 7 services `Running`, smoke clip dubs end to end.
- [ ] `helm upgrade` path still works if ArgoCD is down (tested once by disabling the Application).

Status: `pending`. Depends on: T1–T6 (needs working workers to scale).

### Task 8: Observability — traces, metrics, dashboards, alerts (Wk 8, Nov 9–15 — C+B)

**Outcome:** Every chunk has one trace across all 7 steps, four Grafana dashboards answer "is the SLO holding, what is scaling, which provider is failing, what does it cost", and five alerts fire on real breaches — provable by injecting a breach.

**Why:** Without this, latency, failures, and spend are invisible. This task turns the pipeline from "it works on my machine" into a system whose SLO can be watched and whose failures page someone.

**Build:**
- Tracing: OTel SDK in every service (FastAPI auto-instrumentation + manual spans around SQS send/receive and each provider call). Propagate `chunk_id`/`trace_id` in the SQS envelope. Export OTLP → Collector → Tempo/Jaeger (traces) + Prometheus (metrics). Sampling: 100% dev, 20% under load.
- Metrics (`lingofy_` prefix, Tech Ref §6.2): `stage_duration_seconds{stage,provider}` histogram, `stage_failures_total{stage,error_class}`, `queue_depth{queue}`, `queue_wait_seconds{queue}`, `session_e2e_latency_seconds{session_type}` (the SLO metric), `ai_tokens_billed_total{provider,kind}`, `cost_cents_total{tenant,service}`, `circuit_state{provider}` (0=closed,1=half-open,2=open), `dlq_messages_total{queue}`, `drift_ms{lang}`.
- Dashboards (`deploy/observability/*.json`): 1) SLO Overview (E2E p95 vs 3s line, error-budget burn, stage waterfall, throughput), 2) Workers & Scaling (replicas overlaid with queue depth), 3) AI Providers (CB state timeline, failures by class, fallback events), 4) Cost (cost/min-of-audio, chars per tenant, budget remaining).
- Alerts (`deploy/observability/*.yaml`, Tech Ref §6.4): SLOBurn (p95 > 3s for 5m), StageFailureSpike, DLQNonEmpty, CircuitOpen, CostAnomaly — routed via Alertmanager.

**Verify:**
- [ ] Upload 1-min clip → Jaeger shows one trace per chunk with all 7 spans; Grafana SLO panel shows p95 < 3s.
- [ ] Inject 4s artificial TTS delay → SLOBurn alert fires within 5 min, then resolves when delay is removed.
- [ ] All four dashboards import cleanly on a fresh Grafana (`provisioning` check in CI).

Status: `pending`. Depends on: T1–T7.

### Task 9: Resilience — breaker, DLQ redrive, chaos, auto-remediation (Wk 9, Nov 16–22 — A)

**Outcome:** Killing a worker or blacking out a provider mid-stream loses zero chunks: traffic fails over automatically, poison messages land in the DLQ with a documented redrive, and routine recoveries (scale bump, redrive, fallback check) run as Jobs triggered by alerts — with the runbook describing the manual fallback for each.

**Why:** Paid APIs return 429s and 5xxs; pods get evicted. This task converts those inevitabilities from demo-ending incidents into the demo itself (kill ElevenLabs on stage, audio keeps playing).

**Build:**
- Create `services/ai-gateway/circuit.py`: per-provider breaker — Closed → Open after 5 consecutive failures in 30s; Open for 30s; Half-Open probes with 1 real request; 2 consecutive successes → Closed. Expose `lingofy_circuit_state`. Fallback routing from Task 5 now keys off breaker state, not just HTTP codes.
- DLQ: single shared `lingofy-dlq` (14-day retention). Document redrive via console and `aws sqs start-message-move-task`. Add `deploy/observability/remediation-jobs.yaml`: three bounded, audited Jobs — `remediate-scale-bump` (on SLOBurn: raise KEDA maxReplicas +50% for 15 min), `remediate-dlq-redrive` (on DLQNonEmpty after breaker closes for 5 min), `remediate-fallback-verify` (on CircuitOpen: synthesize 1 canary chunk via fallback and publish event). Wire Alertmanager webhooks to them.
- Create `tests/chaos/*.sh` + `docs/runbook.md` (full): worker-kill (`kubectl delete pod` mid-stream → SQS redelivery), provider-blackout (`iptables`/`tc netem` on gateway egress → CB + Piper), node-drain (GitOps redeploy, RTO ≈ 8 min), poison-chunk (3 deliveries → DLQ → inspect → fix slicer → redrive), partial S3 upload (assembler marks degraded).

**Verify:**
- [ ] Blackout test: block ElevenLabs for 5 min during a 3-min dub → 0 dropped chunks, CB metric hits 2 then recovers, fallback counter > 0.
- [ ] Worker-kill test: delete 2 worker pods mid-burst → output manifest still complete, DLQ stays empty.
- [ ] Auto-redrive test: inject 3 poison messages → DLQ alert fires → recovery Job redrives after breaker closes → DLQ drains.
- [ ] `docs/runbook.md` lists detection → automated response → manual follow-up per failure (Tech Ref §9 table filled in).

Status: `pending`. Depends on: T4–T8.

### Task 10: Cost metering + image signing (Wk 10, Nov 23–29 — A+C)

**Outcome:** Every dashboard shows spend per minute of source audio per tenant, over-budget tenants are automatically forced onto free fallbacks, and no unsigned container image can reach the cluster.

**Why:** AI APIs bill per character/minute. Without metering the project can burn free tiers silently; without signing, a tampered image can ship unnoticed. Both are cheap to add now and expensive to retrofit.

**Build:**
- Create `services/ai-gateway/prices.yaml` (Deepgram $/min, Whisper $0.006/min, DeepL $25/M chars, Google $20/M, ElevenLabs plan chars, Piper $0) + metering middleware: every gateway response increments `lingofy_ai_tokens_billed_total{provider,kind}` and `lingofy_cost_cents_total{tenant,service}`. Budget guard: per-tenant per-day default $2 — exceeded → TTS forced to Piper, ASR capped, alert fired. Cost panel shows primary-vs-fallback delta and budget remaining.
- CI/CD: add `cosign sign` (keyless OIDC) after `docker build` in `ci.yaml`; add `cosign verify` gate at the top of `cd.yaml` (unsigned → deploy refused). Keep existing `trivy --severity CRITICAL,HIGH --exit-code 1` block.

**Verify:**
- [ ] `pytest tests/unit/test_cost_golden.py -v` passes: fixture usage × `prices.yaml` matches expected cents within 5%.
- [ ] Live check: dub 5-min EN → ES+HI with Piper → cost panel reads ≈ $0.00–0.06; same clip forced to paid route reads ≈ $0.80; both visible on one graph.
- [ ] Tamper test: push an unsigned image tag → CD refuses to sync (verify gate fails closed).

Status: `pending`. Depends on: T4–T5 (metering hooks), T8 (panels/alerts).

### Task 11: Web UI + full load/chaos pass, optional Go relay (Wk 11, Nov 30–Dec 6 — C, +B for stretch)

**Outcome:** A non-engineer can upload a clip, pick target languages, watch progress, and play each dubbed track from the browser — while the system holds the SLO under a 50-session burst with an empty DLQ.

**Why:** The UI is the demo surface; the load pass is the proof that Tasks 1–10 hold together. The optional Go relay exists only if live-microphone ingest proves shaky under load — it is a bounded, swappable extra, not a rewrite.

**Build:**
- UI in `services/gateway/static/`: file uploader (`POST /v1/sessions/{id}/audio`), live-mic button (`WS /v1/sessions/{id}/stream`), status view (polls `GET /status`: `chunks_total/done`, per-language `done/p95`, `slo_breach_pct`), per-language HLS player (`GET /audio/{lang}/playlist.m3u8`, proxied or 302 to S3).
- Load/chaos: run the full matrix — `tests/load/k6-burst.js` (50 sessions × 3-min audio, assert SLO p95 < 3s), `tests/chaos/*.sh` (kill + blackout + drain). Fix flakes, cap steady-state load at 20 sessions on t3.micro if CPU saturates (document the cap).
- Stretch (only if WS ingest drops frames under k6, or as a bonus): extract `services/ws-relay/*.go` (~300 lines, stdlib + `nhooyr.io/websocket`): WS in, SQS/S3 out, identical envelope, OTel Go SDK `ws.relay` spans. Python gateway keeps REST; relay handles only live frames.

**Verify:**
- [ ] Manual pass: upload 5-min clip → ES+HI, first dubbed chunk plays in < 3s, rest streams progressively, status page matches Grafana.
- [ ] `k6 run tests/load/k6-burst.js` (50 sessions): p95 < 3s, zero DLQ growth, KEDA 1→12 event captured.
- [ ] If Go relay built: same 50-session run passes with relay in path; Python-only path still passes with relay disabled.

Status: `pending`. Depends on: T1–T10.

### Task 12: Freeze, rehearsal, demo (Wk 12, Dec 7–13 — all)

**Outcome:** A frozen, rehearsed system that runs the 6-scenario demo live twice without surprises — plus a video backup and a one-command teardown for the optional demo-week cluster.

**Why:** Demo week punishes unrehearsed moving parts. Freezing features, rehearsing rollbacks, and pre-recording a backup converts "hope it works" into "already ran twice".

**Build:**
- Freeze: no feature merges after Dec 8; only SLO/crash fixes. Rehearse `helm rollback` and `argocd app rollback` until both are muscle memory.
- Create `scripts/demo.sh` (`./scripts/demo.sh samples/sample-en-5min.mp3 --targets es,hi` prints per-language playlist URLs + Grafana link) and `scripts/destroy-demo-week.sh` (tears down optional EKS burst ≈ $12: control plane $0.10/h × 72h + 2× t3.small; default stays on free-tier k3s t3.micro).
- Polish `README.md` final, record backup video of all 6 scenarios, print a one-page run sheet (order + who clicks what + rollback cue per step).

**Verify:**
- [ ] Full 6-scenario run passes live **twice** in a row: 1) cold start → first dubbed chunk < 3s, 2) k6 burst → KEDA 1→12 and back, 3) ElevenLabs blocked → Piper takeover with no drops, 4) worker pod deleted → redelivery, DLQ empty, 5) cost panel before/after phrase cache, 6) alert-triggered auto-remediation (scale bump + redrive + fallback check) visible on the wall.
- [ ] Video backup plays end to end; `destroy-demo-week.sh --dry-run` shows only the burst resources.

Status: `pending`. Depends on: T1–T11.

---

## Risk register (plain language)

| # | Risk | Likelihood | Impact | What we do about it | Owner | Early warning |
|---|---|---|---|---|---|---|
| R1 | Deepgram $200 free credit runs out mid-semester | Med | High — no ASR | Evaluate local `whisper.cpp` in week 9 as an emergency ASR path; keep paid Whisper fallback behind the $2/day budget guard so it cannot surprise-bill | B | credit balance < 20% |
| R2 | Single t3.micro CPU saturates under load tests | High | Med — SLO misses under burst | Cap steady-state tests at 20 concurrent sessions; record the 50-session scale-up at lower concurrency if needed; use the optional EKS burst only for demo week | A | node CPU > 80% for 5 min |
| R3 | ElevenLabs ~10K free chars run out | High | Med — forced onto lower-quality voice | Piper is the default voice in `values-dev.yaml`; ElevenLabs is used only for short showcase clips; budget guard auto-forces Piper when quota is near | B | usage > 80% of quota |
| R4 | SQS throttling or unexpected charges at 50-session burst | Low | Low | Free tier covers 1M requests/month (our semester fits inside it); KEDA `cooldownPeriod: 120` prevents scale flapping; all dev loops run against localstack, not real SQS | A | queue age growing while replicas maxed |
| R5 | A real provider outage lands on demo day | Low | High | Breaker + Piper fallback is already the live path (not a special mode); DLQ redrive is rehearsed; backup video covers total-blackout case | A | `circuit_state == 2` for 5 min |

## Demo script (10-minute narrative, what the room sees)

1. **Provision** — `./scripts/demo.sh` pre-reqs: `terraform apply` creates queues/buckets (empty → ready). Narrate: "one command, same result every time."
2. **Deploy** — `argocd app sync lingofy` → all pods green. Narrate: "the cluster converges to what is in git."
3. **Scale** — start k6 burst; room watches Grafana Workers panel: queue depth spikes, replicas climb 1→12, then drain back to 1. Narrate: "chunks, not sessions, are the scaling unit."
4. **Break the provider** — `iptables` blocks ElevenLabs; room watches CB panel flip to open, fallback counter rise, audio keep playing via Piper. Narrate: "the paid API failed; nobody listening could tell."
5. **Kill a worker** — `kubectl delete pod` mid-stream; room watches redelivery, complete manifest, empty DLQ. Then show the cost panel: same clip priced on paid vs fallback route.
6. **Self-heal** — point at the remediation events: SLO burn bumped replicas, DLQ redrove itself after recovery, fallback canary verified. Close with the one-liner: "The AI is rented; everything you watched recover is what we built."

## Out of scope (v1)

Voice cloning (fits later as an ElevenLabs `voice_id` through the same gateway), speaker diarization, video lip-sync, multi-node Kubernetes, real WebRTC SFU (WebSocket meets the SLO), multi-region.

## Decision log pointer

Append-only log lives in `TECHNICAL_REFERENCE.md` §15. If this plan needs content not in the Tech Ref, fix the Tech Ref first — do not let the two drift.
