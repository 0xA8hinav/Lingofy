# Lingofy Implementation Plan (MVP Build)

> Track progress with the checkboxes (`- [ ]`) under each task. Work tasks in order — each one assumes the previous ones are done.

**Goal:** Ship a working dubbing pipeline that holds p95 end-to-end latency under 3 seconds and demos five things live: dubbed audio plays, latency holds on Grafana, KEDA scales on load, provider kill fails over to Piper, pipeline runs green from CI.

**Scope rule:** `README.md` + `docs/TECHNICAL_REFERENCE.md` + `docs/architecture.md` describe the **target architecture** (full 7-service design — that is intentional, it shows design thinking at zero build cost). **This plan builds only the MVP core** — the shaded subset below. Anything not in this plan is documented as phased roadmap, not missing work.

```
Target (docs):  gateway → slicer → asr-q → translate-q → tts-q → assembler → HLS
                       + AI Gateway service + 4 dashboards + 3-state breaker + auto-remediation

MVP (this plan): gateway(+slicer inside) → dub-tasks queue → dub-worker (ASR+Translate+TTS inside)
                       → S3 → assembler → HLS   +  retry helper + dumb fallback + 1 dashboard + 2 alerts
```

**The six things that make it a DevOps project, plus one resilience demo:**
1. Containerized services (Docker, 3 images)
2. CI/CD — GitHub Actions: test → build → scan → deploy
3. Small IaC — Terraform for SQS/S3/ECR (~200 lines)
4. Orchestration — k3s + Helm
5. Autoscaling — KEDA on **one** queue, shown under load
6. Observability with **one SLO** — Prometheus + Grafana, one dashboard, one latency panel
7. Plus one: retries + Piper fallback, shown live by killing the primary provider

**One-sentence test for every feature:** *"Does this appear in one of my five demo moments?"* If no → docs-only / future work. Per-stage histograms stay (SLO panel needs them). Half-open breaker probing, trace waterfalls, second/third dashboards, remediation controller → cut.

**Tech Stack (MVP):** Python 3.12 · FastAPI · ffmpeg · boto3 · k3s/k3d · KEDA (one ScaledObject) · Helm (`helm upgrade` deploys; ArgoCD is stretch) · GitHub Actions · Trivy (cosign: 30-min attempt, drop if it fights) · Prometheus · Grafana (1 dashboard) · Alertmanager (2 alerts) · k6 · AWS (SQS ×2, S3, ECR)

**Spec:** `docs/TECHNICAL_REFERENCE.md` is the design reference. Where this plan deliberately simplifies (fewer services/queues/dashboards, dumb breaker), **this plan wins** — the Tech Ref §15 records the simplification, the full design stays as target.

## Global Constraints (MVP)

- p95 latency < 3.0s on clock **enqueue → dubbed chunk playable** (`lingofy_session_e2e_latency_seconds`); upload time tracked separately as `upload_seconds` and excluded. SLO demo runs on a pre-uploaded sample. One SLO alert if breached 5 min.
- Drift ≤ 250 ms; DLQ must stay drainable (second alert on any DLQ ingress).
- Spend ≤ $50; Piper is default voice so paid TTS is opt-in per clip.
- Python mono-stack, one base `python:3.12-slim`, `ruff + pytest`; `pytest` ≥ 70% on `services/` (80% was overkill for the timeline).
- No provider keys in worker env files in git — dev keys via local `.env` (gitignored), demo keys via K8s Secret. (Full OIDC→SSM wiring is phased roadmap.)
- Histogram buckets `0.05,0.1,0.2,0.4,0.8,1.6,3.2,6.4`.
- `minReplicaCount: 1` on the single worker.

---

## How to read this plan

Each task: **Outcome** (what exists when done) → **Why** (which demo moment needs it) → **Build** (exact files) → **Verify** (command + pass line). Owners: A = Aarav (IaC, KEDA, Helm, CI/CD), B = Abhinav (gateway, worker, providers, Piper), C = Anshuman (UI, k6, chaos scripts, dashboard).

## File map (MVP — 3 images, 2 queues, 1 dashboard)

- `services/gateway/` — FastAPI (`app.py`: sessions, upload URL, `WS /stream` skeleton, `/healthz` `/readyz`, `/status`, `/manifest`) + `chunker.py` (ffmpeg 8s silence-snapped, runs **inside gateway**, no separate slicer deployment) + static UI in `static/` (week 11)
- `services/dub-worker/` — **one** worker: `worker.py` (poll `dub-tasks` → ASR → Translate → TTS sequentially per chunk, stage timers), `providers.py` (retry helper + dumb breaker + fallback routing), `prices.py` (one cost counter), Piper voices baked into image
- `services/assembler/` — small loop: watch `s3://output/{session}/` every 10s, sort by `seq`, `atempo` if >12% off, rewrite `playlist.m3u8` + `manifest.json` (missing chunk >90s → `unavailable`, non-blocking)
- `deploy/terraform/` — S3 ×2, SQS ×2 (`dub-tasks` + `lingofy-dlq` with redrive policy), ECR ×3, IAM ×3 (~200 lines total)
- `deploy/helm/lingofy/` — umbrella + 3 subcharts, `values-dev.yaml` / `values-demo.yaml`
- `deploy/keda/` — **one** `ScaledObject` (dub-worker on `dub-tasks`, `queueLength: 5`, min 1 max 8, cooldown 120) + `TriggerAuthentication`
- `deploy/observability/` — `dashboard.json` (**one** dashboard, four panels), `alerts.yaml` (**two** alerts: SLOBurn, DLQNonEmpty)
- `.github/workflows/` — `ci.yaml` (ruff → pytest → build → trivy → kind smoke), `cd.yaml` (`helm upgrade` + integration test + `helm rollback` on fail). No nightly workflow — chaos is two manual scripts.
- `scripts/` — `demo.sh`, `load.sh`, `chaos/kill-worker.sh` (`kubectl delete pod -l app=dub-worker`), `chaos/block-provider.sh` (`iptables` block ElevenLabs)
- `tests/` — `unit/` (chunker, atempo math, breaker, price math), `integration/` (1-min clip → 2 langs < 120s), `load/k6-burst.js`

Phased roadmap (docs-only, not built now): split worker per stage with stage queues, AI Gateway as separate service, OTel manual traces, dashboards 2–4, 3-state breaker with half-open probing, auto-remediation Jobs, ArgoCD (stretch wk 10–11 if ahead), cosign (attempt once), per-tenant budget guards.

---

## Timeline (12 weeks — builds finish week 10, buffer + demo after)

| Wk | Dates | Milestone | Owner |
|---|---|---|---|
| 1 | Sep 21–27 | Repo + ADRs + Terraform skeleton + CI skeleton | A |
| 2 | Sep 28–Oct 4 | Gateway (sessions, upload, status, manifest) + chunker inside | B |
| 3 | Oct 5–11 | Queue + dub-worker v0 (ASR → text, retry helper) | A+B |
| 4 | Oct 12–18 | Worker v1 (Translate + TTS + Piper fallback + dumb breaker) | B |
| 5 | Oct 19–25 | Assembler + HLS + 1-min clip plays end to end | A |
| 6 | Oct 26–Nov 1 | Helm + KEDA on one queue + k6 burst shows 1→8 | A |
| 7 | Nov 2–8 | One dashboard + two alerts + structured logs | C+B |
| 8 | Nov 9–15 | Cost counter + panel + CI green + chaos scripts | A+C |
| 9 | Nov 16–22 | Integration hardening: 5-min clip, kill tests, runbook | all |
| 10 | Nov 23–29 | UI (upload + player + status) + buffer / ArgoCD stretch | C (+A) |
| 11 | Nov 30–Dec 6 | Freeze + rehearsal + video backup | all |
| 12 | Dec 7–13 | Demo week | all |

Total build ≈ 350–450 person-hours. Weeks 11–12 are buffer by design.

---

### Task 1: Repo + IaC skeleton + CI skeleton (Wk 1 — A)

**Outcome:** Fresh clone has all folders, `terraform plan` previews SQS/S3/ECR cleanly, empty PR goes green.

**Why:** Unblocks everything; locks cost decisions (k3s, SQS, 8s chunks, Piper default) as ADRs before code.

**Build:**
- Docs exist (target arch). Create `deploy/terraform/*.tf` (2 S3, 2 SQS + redrive, 3 ECR, 3 IAM — keep under ~200 lines).
- Create `.github/workflows/ci.yaml` skeleton: `ruff` → `pytest` → `docker build` (3 images) → `trivy --severity CRITICAL,HIGH` → `kind` + `helm template --dry-run=server` + 1-min smoke.

**Verify:**
- [ ] `terraform -chdir=deploy/terraform plan` clean (no apply yet).
- [ ] Empty PR → CI green.
- [ ] ADRs (k3s, SQS, 8s, Piper default, 3-service MVP) in README §6.

Status: `pending`.

### Task 2: Gateway + chunker inside it (Wk 2 — B)

**Outcome:** `POST /v1/sessions` → upload → `GET /status` / `GET /manifest` works; 5-min MP3 becomes ~38 16kHz mono WAVs in S3 with `{session_id, chunk_id, seq, start_ms, end_ms}` + one `dub-tasks` message per chunk.

**Why:** Demo moment 1 (dubbed audio plays) starts here; the worker (T3) needs session IDs, S3 keys, and the envelope frozen.

**Build:**
- `services/gateway/app.py`: `POST /v1/sessions`, `POST /{id}/audio`, `WS /{id}/stream` (skeleton), `GET /{id}/status`, `GET /{id}/manifest`, `/healthz`, `/readyz` (SQS+S3 ping). Session store: SQLite on PVC with **replicas: 1** (same interface later swappable to Postgres; never scale past 1 on SQLite or status reads 404 across PVCs).
- `services/gateway/chunker.py`: `silencedetect=noise=-35dB:d=0.4`, snap boundaries ±1.5s, else fixed `-f segment -segment_time 8`. Runs as function call after upload (no separate deployment/queue).

**Verify:**
- [ ] `pytest tests/unit/test_chunker.py` — 5-min clip → 38±2 chunks, boundaries ≤1.5s from silence.
- [ ] `pytest tests/unit/test_gateway_contract.py` — create → upload 10s → status shows chunks; `openapi.json` committed.

Status: `pending`. Depends on: T1.

### Task 3: Queue + dub-worker v0 — audio in, text out (Wk 3 — A+B)

**Outcome:** One chunk on `dub-tasks` returns `TranscriptionDone` (text + confidence) in <1s p50 with retries, no provider key in the worker repo.

**Why:** First crossing of queue → worker → paid API → next step. Proves envelope, visibility timeouts, and retry helper that T4 reuses for Translate/TTS.

**Build:**
- Apply Terraform for real (2 queues: `dub-tasks` visibility 60s maxReceive 3 → DLQ, 4-day retention; localstack mirrors names locally).
- `services/dub-worker/worker.py` v0: long-poll (10s, 1 msg), download WAV, call Deepgram **streaming** endpoint (never pre-recorded REST — exceeds 20s), publish intermediate result forward. `providers.py` v0: 2 retries, full-jitter `random(0, min(2^attempt, 8))s` on 429/5xx/timeout only, timeouts 2s connect / 15s read.
- Log JSON lines with `chunk_id` from day one (this replaces manual OTel traces).

**Verify:**
- [ ] `pytest tests/integration/test_asr_e2e.py` — 1 chunk → text in <1s p50.
- [ ] Delete worker pod mid-call → message redelivers, no loss/dupe downstream.
- [ ] `grep -ri deepgram services/dub-worker --include=*.py | grep -i key` shows keys only read from env/Secret, never hardcoded.

Status: `pending`. Depends on: T1–T2.

### Task 4: Worker v1 — Translate + TTS + dumb fallback (Wk 4 — B)

**Outcome:** Text fans out to ES+HI audio; blocking ElevenLabs still yields complete audio via Piper. This is demo moment 4.

**Why:** The dub itself + the kill-provider story. Built now (not later) so fallback is the normal path, not a patch.

**Build:**
- Extend `worker.py`: per chunk, sequential `ASR → Translate (DeepL, 1→N per lang) → TTS (ElevenLabs `eleven_turbo_v2`)`, with per-stage timers feeding `stage_duration_seconds{stage}`. Emit `chars_billed` per call for cost.
- `providers.py` v1 — dumb breaker (no half-open): 3 consecutive failures for a provider → route that provider to fallback for 60s, then retry primary once. TTS fallback: Piper binary + voices (`es_ES-mls_1024-low`, `hi_IN-pratham-medium`) baked into the worker image (not a sidecar). ASR fallback: paid Whisper behind a flag (default off); Translate fallback: Google (flag, default off — document, don't fully wire if time is short).
- Write `s3://audio/{session}/{lang}/{chunk}.mp3` per language.

**Verify:**
- [ ] `pytest tests/integration/test_dub_1min.py` — 1-min EN → ES+HI playable MP3s.
- [ ] `./scripts/chaos/block-provider.sh --provider elevenlabs` during a dub → all chunks finish with `provider=piper`, fallback counter rises, zero drops. Be ready to say in defense: "production would add half-open probing."

Status: `pending`. Depends on: T3.

### Task 5: Assembler + HLS — chunks become a watchable track (Wk 5 — A)

**Outcome:** Per-language MP3s become `playlist.m3u8` + `manifest.json` with drift ≤250 ms; one missing chunk never blocks the session.

**Why:** Workers emit out-of-order, variable-length audio. Without reorder + time-stretch + playlist, demo moment 1 is a folder of files, not a playing track.

**Build:**
- `services/assembler/assemble.py`: poll `s3://audio/{session}/{lang}/` every 10s, sort by `seq`, check contiguity, `atempo 0.9–1.1` when dubbed/source differ >12%, write `s3://output/{session}/{lang}/playlist.m3u8` (2s segments) + `manifest.json`. Missing chunk past 90s → mark `unavailable`, finalize anyway.

**Verify:**
- [ ] 5-min sample → `drift_ms` p95 ≤ 250 ms.
- [ ] Shuffle 10 chunks → playlist still in `seq` order; withhold 1 chunk 90s → manifest finalizes with 1 `unavailable`.

Status: `pending`. Depends on: T4.

### Task 6: Helm + KEDA on one queue + load burst (Wk 6 — A)

**Outcome:** `helm upgrade` deploys all 3 services; k6 burst grows `dub-worker` 1→8 on backlog and drains back. This is demo moment 3 (scale).

**Why:** The elastic-pipeline claim with the smallest moving part: one ScaledObject, one queue.

**Build:**
- `deploy/helm/lingofy/` (umbrella + 3 subcharts, `values-dev.yaml`/`values-demo.yaml`), `deploy/keda/`: one `TriggerAuthentication` + one `ScaledObject` (`queueLength: 5`, min 1 max 8, cooldown 120, poll 10). Worker requests `100m/256Mi`, limits `500m/512Mi` — fits t3.micro (2 vCPU); throttling in the burst screenshot is cosmetic, note it on the graph.
- `tests/load/k6-burst.js` + `scripts/load.sh`: ramp to 30–50 concurrent sessions of short audio (cap steady-state at 20 on t3.micro if CPU saturates — document the cap).

**Verify:**
- [ ] `./scripts/load.sh --sessions 40` → Grafana shows 1→8 replicas, backlog → ~0 within 60s of burst end (screenshot saved).
- [ ] Fresh `k3d` + `helm upgrade --install` → smoke clip dubs E2E.

Status: `pending`. Depends on: T1–T5.

### Task 7: One dashboard + two alerts + logs (Wk 7 — C+B)

**Outcome:** One Grafana dashboard answers all four questions (SLO? scaling? providers? cost?) and two alerts catch the only two pages that matter.

**Why:** Demo moment 2 (latency holds on Grafana). One good dashboard beats four half-filled ones; per-stage histograms give the waterfall without tracing infra.

**Build:**
- Metrics in worker/gateway: `stage_duration_seconds{stage}` + `session_e2e_latency_seconds` histograms, `queue_depth`, `dlq_messages_total`, `fallback_total{provider}`, `cost_cents_total`. SQS gauge source: worker calls `GetQueueAttributes` on both queues every poll loop and exposes the numbers — Prometheus cannot see SQS alone (prevents a week-7 stall).
- `deploy/observability/dashboard.json` — **one** dashboard, four panels: E2E p95 vs 3s line, per-stage p50/p95 (from histograms), replicas vs queue depth, cost/min.
- `deploy/observability/alerts.yaml` — **two** alerts only: SLOBurn (p95>3s 5m), DLQNonEmpty (any ingress 10m). Structured JSON logs with `chunk_id` everywhere.

**Verify:**
- [ ] 1-min clip → dashboard shows p95 < 3s + stage breakdown + cost row.
- [ ] Inject 4s TTS delay → SLOBurn fires ≤5 min, resolves on removal. Push 1 poison msg → DLQ alert fires.

Status: `pending`. Depends on: T1–T6.

### Task 8: Cost counter + green CI + chaos scripts (Wk 8 — A+C)

**Outcome:** Cost per minute of audio is visible; CI blocks bad images; two one-command chaos scripts reproduce the demo failures.

**Why:** Cost panel is the highest value-per-hour feature (keep it); Trivy covers security without cosign fights; manual scripts replace the nightly workflow with zero demo loss.

**Build:**
- `services/dub-worker/prices.py` + `prices.yaml` (Deepgram $/min, DeepL $25/M chars, ElevenLabs plan, Piper $0) → `cost_cents_total` + cost panel. No per-tenant guards — note as "config + check" in future work.
- `ci.yaml` final: ruff → pytest → build 3 images → `trivy --severity CRITICAL,HIGH --exit-code 1` → kind smoke (1-min clip < 120s). Attempt `cosign sign` once for 30 min; keep if trivial, drop with a one-line note if not.
- `scripts/chaos/kill-worker.sh`, `scripts/chaos/block-provider.sh` (each < 30 lines, echo what they do and how to undo).

**Verify:**
- [ ] 5-min EN→ES+HI via Piper reads ≈ $0.00–0.06; forced paid route ≈ $0.80 — both on one graph; golden test `test_cost_golden.py` within 5%.
- [ ] `trivy` with a known-bad base fails the PR; both chaos scripts run and undo cleanly.

Status: `pending`. Depends on: T4, T7.

### Task 9: Hardening week — 5-min clip + kill tests + runbook (Wk 9 — all)

**Outcome:** The 5-minute two-language clip dubs reliably, both kill tests pass back-to-back, and the runbook fits on two pages.

**Why:** Buffer against the classic failure (six services, none integrated week 10). No new features this week — only reliability.

**Build:**
- Fix flakes found in T8 runs; tune `queueLength`/timeouts/ffmpeg flags; write `docs/runbook.md` (detection → auto response → manual step per row: 429, 5xx/CB-open, OOM, poison chunk → DLQ → `aws sqs start-message-move-task` manual redrive, partial S3, node loss → `helm upgrade` redeploy RTO ~8 min).
- Manual DLQ redrive only (no auto-Job).

**Verify:**
- [ ] 5-min clip → 2 langs, drift ≤250 ms, p95 < 3s, DLQ empty — three runs in a row.
- [ ] Block-provider 5 min + delete 2 worker pods mid-burst → complete manifest, DLQ empty.

Status: `pending`. Depends on: T1–T8.

### Task 10: UI + buffer / ArgoCD stretch (Wk 10 — C, +A if ahead)

**Outcome:** A non-engineer can upload, pick languages, watch progress, and play each track. If ahead, deploys move to ArgoCD; if not, Helm stays and ArgoCD is a slide.

**Why:** Demo moment 1 needs a friendly surface. ArgoCD is explicitly stretch — `helm upgrade` in CI is defensible, ArgoCD only if week 10 is green by Wednesday.

**Build:**
- `services/gateway/static/`: uploader, language picker, status view (`GET /status`), per-language HLS player via **hls.js** (native HLS is Safari-only — include the library or playback fails outside Safari).
- Stretch only: `deploy/argocd/Application.yaml` watching `deploy/helm/lingofy`; `argocd app sync` replaces `helm upgrade`. Timebox: 2 days, revert to Helm on friction.

**Verify:**
- [ ] Stranger test: new user dubs a clip with no help; first chunk plays < 3s.
- [ ] 30-session burst through the UI path holds SLO.

Status: `pending`. Depends on: T1–T9.

### Task 11: Freeze + rehearsal + backup (Wk 11 — all)

**Outcome:** Frozen system, practiced rollbacks, video backup, one-page run sheet.

**Why:** Demo week punishes moving parts. Freeze early, rehearse failure, pre-record.

**Build:**
- Freeze features; crash/SLO fixes only. Rehearse `helm rollback` until muscle memory. Record backup video of all five moments. Print run sheet (order, who clicks, rollback cue per step). Optional EKS burst decided here (~$12) else stay on free-tier k3s.

**Verify:**
- [ ] Full 5-moment run passes twice in a row; video plays E2E.

Status: `pending`. Depends on: T1–T10.

### Task 12: Demo week (Wk 12 — all)

**Outcome:** Live demo + teardown in one command.

**Build:**
- `scripts/demo.sh samples/sample-en-5min.mp3 --targets es,hi` (prints playlist URLs + Grafana link), `scripts/destroy-demo-week.sh`.

**Verify:**
- [ ] Live run green; burst resources torn down.

Status: `pending`. Depends on: T11.

---

## What was cut (stays in target docs, not built now)

| Full-design item | MVP replacement | Defense line |
|---|---|---|
| 7 services, queue per stage | 3 deployments, 1 work queue (`dub-tasks`) + DLQ; stage timers inside worker | "Same histogram data, no extra infra; split when backlog justifies it" |
| AI Gateway as service | `providers.py` module in worker (retry + dumb breaker) | "Extract to service when a second caller exists" |
| OTel manual traces | Prometheus histograms + JSON logs with `chunk_id` | "Numbers first; traces when we chase cross-service tails" |
| 4 dashboards, 5 alerts | 1 dashboard (4 panels), 2 alerts | "One screen tells the demo story" |
| 3-state breaker with half-open | 3 failures → fallback 60s, retry once | "Half-open probing is the production upgrade" |
| Nightly chaos workflow | `kill-worker.sh` + `block-provider.sh`, manual | "Same fault coverage, zero scheduler to maintain" |
| Per-tenant budgets, anomaly alerts | One `cost_cents_total` + one panel | "Budgets are config + a check on top of this counter" |
| DLQ auto-redrive | Manual `start-message-move-task`, runbooked | "Automation after the second manual redrive" |
| ArgoCD | Stretch wk 10, else `helm upgrade` | "Declarative deploys; GitOps sync is the next step" |
| Remediation controller | Future-work slide | "Cut as gold-plating; alert → runbook → Job is the sequence" |
| cosign | Attempt 30 min, Trivy stays | "Trivy blocks bad images today; signing when release needs it" |

## Risk register

| # | Risk | Likelihood | Impact | Mitigation | Owner | Signal |
|---|---|---|---|---|---|---|
| R1 | Deepgram credit exhausted | Med | High | Whisper flag + local `whisper.cpp` noted for wk 9; Piper default keeps TTS free | B | credit < 20% |
| R2 | t3.micro saturates | High | Med | Cap steady load at 20 sessions; burst to 40 only for the scale screenshot; EKS burst optional | A | CPU > 80% 5m |
| R3 | ElevenLabs quota exhausted | High | Med | Piper default in `values-dev`; ElevenLabs for showcase clips only | B | chars > 80% |
| R4 | Single worker becomes bottleneck | Med | Med | KEDA max 8 + `queueLength: 5` tuning; split worker by stage is the documented next step | A | queue age grows at max replicas |
| R5 | Real provider outage on demo day | Low | High | Dumb fallback is the normal path; video backup covers total blackout | A | breaker open 5 min |

## Demo script (10 minutes, five moments)

1. **Play** — upload 5-min EN → ES+HI via UI (pre-upload before timing); first dubbed chunk plays < 3s on the enqueue→playable clock, rest streams. (Shows: pipeline works.)
2. **Hold SLO** — Grafana: E2E p95 vs 3s + per-stage bars, measured on the pre-uploaded sample so college Wi-Fi upload time never enters the SLO. (Shows: numbers, not claims.)
3. **Scale** — k6 burst; replicas 1→8 on queue depth, drain back. (Shows: elasticity.)
4. **Kill provider** — block ElevenLabs; fallback counter rises, audio continues via Piper. (Shows: resilience.)
5. **Stay green** — `kubectl delete pod` mid-stream → redelivery, complete manifest, empty DLQ + cost panel paid-vs-fallback. (Shows: operability + cost control.)

Narrative spine: *"The AI is rented; everything you watched hold, scale, fail over, and recover is what we built."*

## Out of scope (v1)

Voice cloning (`voice_id` later through same provider module), diarization, lip-sync, multi-node K8s, WebRTC SFU, multi-region.

## Decision log pointer

Full design + prior decisions: `TECHNICAL_REFERENCE.md` §15. MVP simplifications above are intentional and recorded here — if this plan needs something not in the Tech Ref, fix the Tech Ref first.
