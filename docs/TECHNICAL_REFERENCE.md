# Lingofy — Technical Reference

> Scope: this doc details the full design (per-stage services/queues, separate AI Gateway, full observability). The MVP build in `docs/plan.md` implements a subset — gateway(+chunker), one `dub-worker`, assembler, 1 work queue + DLQ, 1 dashboard, dumb fallback. Precedence: **plan.md wins for build**; this doc is design reference. `README.md` §15 and `docs/plan.md` §"What was cut" map the gap; §15 logs MVP simplifications.

---

## 1. Service specifications

### 1.1 `gateway` — API Gateway

| Field | Value |
|---|---|
| Tech | FastAPI (Python 3.12), uvicorn, websockets |
| Port | 8080 |
| Replicas | 1 (MVP — single replica avoids SQLite-on-PVC split-brain; HPA is future scope) |
| Endpoints | see §2.2 |
| Env | `AWS_REGION`, `S3_BUCKET_AUDIO`, `SQS_DUB_URL`, `JWT_SECRET` (MVP; OTel exporter and OIDC→SSM wiring are future scope) |
| Health | `GET /healthz` (liveness), `GET /readyz` (checks SQS + S3 reachability) |
| Notes | MVP: single replica with SQLite on PVC (same interface later swappable to Postgres/DynamoDB). Do not scale past 1 on SQLite — status reads would 404 across PVCs. |

### 1.2 `slicer` — Chunker

| Field | Value |
|---|---|
| Tech | Python + `ffmpeg` binary baked into image (`ffmpeg:6-alpine` base) |
| Trigger | Target: SQS `ingest-tasks` or direct call. MVP: plain function inside gateway, no queue (see `services/gateway/chunker.py`) |
| Chunking | Target 8s. Smart split: run `ffmpeg -i in -af silencedetect=noise=-35dB:d=0.4 -f null -` to find silence points, snap chunk boundaries to nearest silence within ±1.5s; fall back to fixed `-f segment -segment_time 8` when no silence found |
| Output | Per chunk: S3 object `s3://audio/{session_id}/{chunk_id}.wav` (16 kHz mono PCM WAV for ASR friendliness) + `ChunkCreated` message |
| Manifest | Append record to session manifest in object store (last-write-wins with seq lock via conditional PUT) |

ffmpeg split example (fallback fixed mode):
```bash
ffmpeg -i input.wav -vn -ac 1 -ar 16000 \
  -f segment -segment_time 8 -reset_timestamps 1 \
  chunk_%04d.wav
```

### 1.3 `worker-asr` (target — MVP folds into `dub-worker`)

| Field | Value |
|---|---|
| Consume | SQS `asr-tasks` (long poll 10s, max 1 msg) |
| Call | Deepgram streaming/pre-recorded via AI Gateway; **must use Deepgram's low-latency endpoint — its pre-recorded REST endpoint can exceed 20 s and is unsuitable for the 3s SLO**; streaming returns transcripts in <300 ms |
| Emit | `TranscriptionDone` → `translate-tasks` |
| Failure | visibility timeout 60s; maxReceiveCount 3 → DLQ |
| Env | `DEEPGRAM_API_KEY` (via AI GW, not directly), `CHUNK_TIMEOUT_S=30` |

### 1.4 `worker-translate` (target — MVP folds into `dub-worker`)

| Field | Value |
|---|---|
| Consume | `translate-tasks` |
| Call | DeepL REST via AI Gateway (`auth_key` in secret) |
| Fan-out | One input message → **N output messages** (one per target language) on `tts-tasks` |
| Metering | Report `chars_billed` to AI GW response header → cost meter |
| Env | `TARGET_LANGS` (default `es,hi`), `DEEPL_TIER=free` |

### 1.5 `worker-tts` (target — MVP folds into `dub-worker`, Piper baked into image, not sidecar)

| Field | Value |
|---|---|
| Consume | `tts-tasks` |
| Primary | ElevenLabs `eleven_turbo_v2` (fast tier) via AI GW |
| Fallback | **Piper** running as a sidecar container in the same pod (`unix socket`), used when CB for ElevenLabs is open or chars quota exhausted |
| Output | S3 `s3://audio/{session_id}/{lang}/{chunk_id}.mp3` + `SynthesisDone` → `assemble-tasks` |
| Note | Piper voice per target language pinned in config: `es_ES-mls_1024-low`, `hi_IN-pratham-medium` |

### 1.6 `ai-gateway` (target — MVP implements `providers.py` module inside `dub-worker`; extract to service when a second caller exists)

A thin LiteLLM-proxy-style sidecar/service. Every provider call from every
worker goes through it. See §4 for full spec — this is the heart of the
reliability story.

### 1.7 `assembler` (Sync/Assembler)

| Field | Value |
|---|---|
| Consume | `assemble-tasks` |
| Logic | Collect per-language chunk audio; verify contiguity (`end_ms[i] ≈ start_ms[i+1]`); if a dubbed chunk is >12% shorter/longer than source chunk, apply ffmpeg `atempo 0.9–1.1` time-stretch; emit `playlist.m3u8` per language (2s segments) + final `manifest.json` |
| Output | S3 `s3://output/{session_id}/{lang}/playlist.m3u8`, `{session_id}/manifest.json` |
| Edge | Missing chunk past deadline (session-level SLA of 90s) → mark manifest entry `unavailable` rather than blocking |

### 1.8 Optional `ws-relay` (future scope — not in MVP; hardening/UI occupy weeks 9–10 in `plan.md`)

| Field          | Value                                                                                                                                                           |
| ----------------| -----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Tech           | Go, stdlib `net/http` + `nhooyr.io/websocket` or `gorilla/websocket`                                                                                            |
| Responsibility | Sustained binary frame fan-out with backpressure for live-mic ingest; speaks WS on one side, SQS/S3 on the other                                                |
| Contract       | Same message envelope as §2.1; no provider creds; OTel Go SDK for `ws.relay` spans                                                                              |
| When to build  | Only if live WS ingest is shaky under k6 load, or as a bounded polyglot bonus. Default v1 stays Python FastAPI WS.                                              |
| Rationale      | Go's GC predictability and goroutine-per-connection model shine at high fan-out; at 20–50 sessions Python asyncio is sufficient. See `docs/architecture.md` §6. |

## 2. Data contracts

### 2.1 Queue message schema (all queues share envelope)

```json
{
  "schema_version": 1,
  "message_id": "uuid",
  "session_id": "sess_9f3a",
  "chunk_id": "sess_9f3a_0042",
  "seq": 42,
  "start_ms": 336000,
  "end_ms": 344000,
  "lang": "es",
  "s3_key": "audio/sess_9f3a/0042.wav",
  "attempt": 1,
  "trace_id": "otel-trace-id",
  "created_at": "2026-09-18T10:12:33.221Z"
}
```

Per-stage payloads ride in a `payload` object:
- `TranscriptionDone.payload`: `{ "text": "...", "confidence": 0.94, "provider": "deepgram", "asr_ms": 380 }`
- `TranslationDone.payload`: `{ "source_text": "...", "target_text": "...", "provider": "deepl", "chars_billed": 210 }`
- `SynthesisDone.payload`: `{ "audio_s3_key": "...", "duration_ms": 8100, "provider": "elevenlabs|piper", "chars_billed": 210 }`

### 2.2 HTTP API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/sessions` | Create session `{source_lang, target_langs[], ttl_hours}` → `{session_id, upload_url}` |
| POST | `/v1/sessions/{id}/audio` | Multipart upload (file mode) |
| WS | `/v1/sessions/{id}/stream` | Live mic ingest (binary audio frames) |
| GET | `/v1/sessions/{id}/status` | `{chunks_total, chunks_done, languages{es:{done,p95_ms}}, slo_breach_pct}` |
| GET | `/v1/sessions/{id}/manifest` | Timeline map + per-language playlist URLs |
| GET | `/v1/sessions/{id}/audio/{lang}/playlist.m3u8` | HLS playlist (proxied or 302 to S3) |
| DELETE | `/v1/sessions/{id}` | Explicit purge (else TTL reaper after `ttl_hours`) |

### 2.3 Queue design (target — MVP uses `dub-tasks` + `lingofy-dlq` only; same timeouts/retention)

| Queue | Visibility timeout | maxReceiveCount | DLQ | Msg retention |
|---|---|---|---|---|
| `ingest-tasks` | 120 s | 3 | `lingofy-dlq` | 4 d |
| `asr-tasks` | 60 s | 3 | `lingofy-dlq` | 4 d |
| `translate-tasks` | 30 s | 3 | `lingofy-dlq` | 4 d |
| `tts-tasks` | 90 s | 3 | `lingofy-dlq` | 4 d |
| `assemble-tasks` | 60 s | 3 | `lingofy-dlq` | 4 d |
| `lingofy-dlq` | 300 s | — | — | 14 d |

Single shared DLQ keeps ops simple; message envelope's stage is recoverable
from `payload`. **DLQ redrive** (console or `aws sqs start-message-move-task`)
is the documented recovery path after a provider incident. Future scope: auto-redrive Job on Alertmanager webhook — manual redrive in MVP (see plan.md "What was cut").

## 3. Latency budget (implementation view — target spans; MVP uses histograms + JSON logs with `chunk_id`)

SLO clock (MVP): **enqueue → dubbed chunk playable.** Upload time is reported separately (`upload_seconds`) and excluded — a 5-min MP3 over slow Wi-Fi must not blow the SLO. Run the SLO demo on a pre-uploaded sample.

| Stage | p50 target | p95 target | Instrumented as |
|---|---|---|---|
| Gateway → Slicer | 40 ms | 150 ms | span `slicer.chunk` |
| Queue wait | 200 ms | 700 ms | `queue_wait_seconds` (enqueue→consume start) |
| ASR | 220 ms | 400 ms | span `asr.call` |
| Translate | 120 ms | 250 ms | span `translate.call` |
| TTS | 350 ms | 700 ms | span `tts.call` |
| Assemble | 80 ms | 200 ms | span `assemble.chunk` |
| **E2E** | **1.1 s** | **3.0 s** | `lingofy_session_e2e_latency_seconds` |

## 4. AI Gateway spec (target — MVP implements the dumb subset in `providers.py`: 2 retries, 3-fails→fallback-60s, no half-open, no budget guard)

### 4.1 Behavior

| Concern | Implementation |
|---|---|
| Retries | Up to 3 attempts, exponential backoff **with full jitter**: `sleep = random(0, min(2^attempt, 8))s`. Retry only on 429/5xx/timeout. `Retry-After` honored. |
| Circuit breaker | Per provider. Closed→Open after 5 consecutive failures in 30s window; Open for 30s; then Half-Open (probe with 1 real request); 2 consecutive successes → Closed. |
| Fallback | TTS: ElevenLabs → Piper (sidecar). ASR: Deepgram → OpenAI Whisper (paid, budget-gated). Translation: DeepL → Google Translate. Fallback attempts are logged and metered separately. |
| Rate limiting | Token bucket per provider per pod: DeepL free ≈ 60 req/min; ElevenLabs per plan. Overflow → 429 back to worker → SQS retry (natural backoff). |
| Cost metering | Every response emits a metric increment: `lingofy_ai_tokens_billed_total{provider,kind}` and `lingofy_cost_cents_total{tenant,provider}` using a price table from config (`prices.yaml`). |
| Budget guard | Per-tenant per-day budget (default $2). Exceeded → TTS forced to Piper, ASR forced to Whisper local budget cap; alert fired. |
| Timeouts | Connect 2s, read 10s (ASR 15s). Hard kill prevents worker hangs. |

### 4.2 Circuit state metric

`lingofy_circuit_state{provider}` — gauge: 0=closed, 1=half-open, 2=open.
Grafana stat panel + Alertmanager rule on `== 2 for 5m`.

## 5. Autoscaling spec (KEDA + SQS)

KEDA scales worker deployments on SQS backlog using the SQS scaler
(`TriggerAuthentication` + `ScaledObject`); the scaler polls
`ApproximateNumberOfMessages` (+ `NotVisible` when configured) and drives
`minReplicaCount`/`maxReplicaCount` accordingly.

```yaml
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: aws-sqs-auth
  namespace: lingofy
spec:
  podIdentity:
    provider: aws-eks          # on k3s: use accessKey/secretKey from secret
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: dub-worker-scaler      # MVP: single worker, single queue
  namespace: lingofy
spec:
  scaleTargetRef:
    name: dub-worker
  minReplicaCount: 1
  maxReplicaCount: 8
  cooldownPeriod: 120
  pollingInterval: 10
  triggers:
    - type: aws-sqs-queue
      authenticationRef:
        name: aws-sqs-auth
      metadata:
        queueURL: https://sqs.ap-south-1.amazonaws.com/ACC/lingofy-dub-tasks
        queueLength: "5"        # 1 replica per 5 pending msgs
        awsRegion: "ap-south-1"
        identityOwner: operator
```

Tuning notes (MVP):
- `queueLength: 5` → a burst of 40 chunks spawns ~8 replicas.
- `cooldownPeriod: 120` prevents flapping during chunk inter-arrival gaps.
- `dub-worker` `maxReplicaCount: 8`, requests `cpu: 100m, mem: 256Mi`, limits `cpu: 500m, mem: 512Mi` — 100m fits t3.micro (2 vCPU) alongside gateway/assembler/prometheus; expect cosmetic CPU throttling in the burst screenshot.
- Scale-to-zero is disabled (`minReplicaCount: 1`) to keep the demo
  always-warm; document scale-to-zero as the production variant.

Future scope: auto scale-bump Job on SLOBurn webhook — manual tuning in MVP (see plan.md "What was cut").

## 6. Observability spec

### 6.1 Tracing (target — MVP uses JSON logs with `chunk_id` + histograms, no OTel Collector/Tempo)

- OTel SDK + FastAPI auto-instrumentation for HTTP; **manual spans** around
  queue send/receive and each provider call.
- `chunk_id` is the correlation key, propagated in the SQS message envelope.
- Exporter: OTLP → Collector → Prometheus (metrics) + Tempo/Jaeger (traces).
- Sampling: parent-based traceidratio, 100% in dev, 20% in load tests.

### 6.2 Metric catalogue (Prometheus naming: `_total` counters, `_seconds` histograms, `lingofy_` prefix)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `lingofy_ingest_chunks_total` | counter | `source` | chunk production rate |
| `lingofy_stage_duration_seconds` | histogram | `stage`, `provider` | per-stage latency → p50/p95/p99 via `histogram_quantile()` |
| `lingofy_stage_failures_total` | counter | `stage`, `error_class` (`429\|5xx\|timeout\|validation`) | stage success rate |
| `lingofy_queue_depth` | gauge | `queue` | backlog visibility (also drives KEDA). MVP source: worker calls `GetQueueAttributes` (both queues) every poll loop and exposes gauges — Prometheus cannot see SQS alone. |
| `lingofy_queue_wait_seconds` | histogram | `queue` | enqueue→dequeue latency |
| `lingofy_session_e2e_latency_seconds` | histogram | `session_type` | the SLO metric (MVP clock: enqueue → playable; upload tracked separately as `upload_seconds`) |
| `lingofy_ai_tokens_billed_total` | counter | `provider`, `kind` (`chars\|audio_seconds`) | FinOps |
| `lingofy_cost_cents_total` | counter | `tenant`, `service` | cost panels |
| `lingofy_circuit_state` | gauge | `provider` | CB visual + alerts |
| `lingofy_dlq_messages_total` | counter | `queue` | DLQ ingress alarm. MVP source: same `GetQueueAttributes` poll as `queue_depth`. |
| `lingofy_drift_ms` | histogram | `lang` | timeline drift vs source |

Histogram bucket guidance: latency buckets `0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4`
— bounded by the 3s SLO so p95 lands inside a real bucket.

### 6.3 Dashboards (target: 4 below. MVP ships one `dashboard.json` with four panels covering the same questions.)

1. **SLO Overview** — E2E p95 vs 3s line, error-budget burn (gauge), stage
   waterfall, chunk throughput.
2. **Workers & Scaling** — replica count per worker overlaid with queue depth
   (the KEDA money-shot), pod restarts, CPU throttling.
3. **AI Providers** — CB state timeline, failures by class, latency per
   provider, fallback activation events.
4. **FinOps** — cost/min-of-audio, chars billed per tenant, budget remaining,
   fallback-vs-primary cost delta.

### 6.4 Alerts (target table — MVP ships SLOBurn + DLQNonEmpty only)

| Alert | Expr | For | Severity |
|---|---|---|---|
| SLOBurn | `histogram_quantile(0.95, sum(rate(lingofy_session_e2e_latency_seconds_bucket[5m])) by (le)) > 3` | 5m | page |
| StageFailureSpike | `rate(lingofy_stage_failures_total[5m]) > 0.05 * rate(lingofy_stage_duration_seconds_count[5m])` | 5m | page |
| DLQNonEmpty | `increase(lingofy_dlq_messages_total[10m]) > 0` | 0m | ticket |
| CircuitOpen | `max_over_time(lingofy_circuit_state[5m]) >= 2` | 5m | warn |
| CostAnomaly | `rate(lingofy_cost_cents_total[1h]) > 3 * avg_over_time(rate(lingofy_cost_cents_total[1h])[7d:1h])` | 30m | ticket |

Future scope: `SLOBurn` → auto-scale bump Job, `DLQNonEmpty` →
auto-redrive Job, `CircuitOpen` → fallback-verification Job.
Manual runbook is the MVP path.

## 7. Deployment topology

- **Cluster**: k3s single node (k3d in dev; t3.micro free tier in "prod").
- **Namespaces**: `lingofy` (app), `observability`, `infra` (localstack in dev).
- **Resource requests/limits** (MVP `dub-worker`): requests `cpu: 100m, mem: 256Mi`, limits `cpu: 500m, mem: 512Mi` — fits t3.micro (2 vCPU) with gateway/assembler/observability; throttling in burst is cosmetic, note it on the demo graph.
- **IaC**: Terraform for AWS-side only — S3 buckets, MVP 2 SQS queues + DLQ redrive policy (target: 6), ECR repos, IAM roles (least privilege per service).
- **Helm**: umbrella chart `lingofy` with subcharts per service; values split `values-dev.yaml` / `values-demo.yaml`.
- **GitOps (stretch, week 10, only if green by Wednesday)**: ArgoCD `Application` watching the repo; `helm upgrade` stays the MVP deploy path. See `deploy/argocd/` (created only in stretch).
- **Secrets (MVP)**: dev keys via gitignored `.env`, demo keys via plain K8s Secret; never in git. Full OIDC→SSM wiring is future scope. Keys are read only by the worker's provider helper (no separate AI GW service in MVP).

## 8. CI/CD (GitHub Actions)

`ci.yaml` — on PR:
1. `ruff lint` + `mypy` per service
2. `pytest` unit (min 70% lines on services/ for MVP pace)
3. `docker build` per changed service (3 MVP images)
4. `trivy image --severity CRITICAL,HIGH --exit-code 1` (enforced gate)
5. Optional 30-min attempt: `cosign sign` (keyless, OIDC) — keep if trivial, drop with a one-line note if it fights; Trivy stays regardless.
6. `kind` up → `helm template | kubectl apply --dry-run=server` → smoke test
   (publish 1-min sample, assert manifest ready within 120s)
7. Merge queue blocks on smoke-test failure.

`cd.yaml` — on main:
1. Push images to ECR tagged `git-sha`
2. Deploy via `helm upgrade --install` (MVP path; ArgoCD sync only in stretch) then run `tests/integration/`; auto-rollback (`helm rollback`) on failure. Optional `cosign verify` gate only if signing was kept.

No nightly chaos workflow in MVP — chaos is `scripts/chaos/kill-worker.sh` + `block-provider.sh`, run manually.

## 9. Failure modes & runbook (excerpt)

| Failure                      | Detection               | Automated response                                       | Manual follow-up                                 |
| ------------------------------| -------------------------| ----------------------------------------------------------| --------------------------------------------------|
| Provider 429 (quota)         | `error_class=429` spike | GW rate-limiter absorbs; SQS retry backoff spreads load  | raise quota / shift to fallback                  |
| Provider 5xx storm           | CB opens                | traffic → fallback provider; alert `CircuitOpen`         | redrive DLQ after incident, reconcile cost meter |
| Worker OOM                   | pod restarts metric     | K8s restarts; chunk redelivered after visibility timeout | bump limits if recurring                         |
| Poison chunk (corrupt audio) | 3 deliveries → DLQ      | DLQ ingress alert                                        | inspect, fix slicer, redrive                     |
| S3 upload partial            | assembler gap detection | session marked degraded, missing chunk flagged           | re-run slicer for session                        |
| Whole-node loss              | nodes-down alert        | redeploy `helm upgrade` from CI (GitOps recovery)        | document RTO ≈ 8 min in runbook                  |

Future scope (not MVP): bounded remediation Jobs — `remediate-scale-bump`, `remediate-dlq-redrive` (post-recovery), `remediate-fallback-verify`. MVP uses manual redrive + runbook (see plan.md "What was cut").

## 10. Test strategy

| Layer | Tool | What is asserted |
|---|---|---|
| Unit | pytest | chunker boundaries, manifest contiguity, backoff jitter bounds, price-table math |
| Contract | pytest + localstack | message envelope schema; every queue round-trips |
| Integration | pytest against kind | happy path: 1-min clip → 2 languages manifest in < 120s |
| Load | k6 | 20 steady / 40 burst sessions × short audio; assert SLO p95, KEDA 1→8 scaling event captured (t3.micro cap documented) |
| Chaos | bash + kubectl (`kill-worker.sh`, `block-provider.sh`) | worker kill, provider blackout, node drain (manual) |
| Cost | pytest | price table × fixture usage = expected cents (golden files) |
| Security | trivy (enforced); cosign verify only if signing kept | no CRITICAL/HIGH vulns |

## 11. Cost model (detail)

**AWS (steady state)**: k3s on 1× t3.micro = $0 (free tier 750h) · SQS first
1M req/mo = $0 · S3 5GB = $0 · ECR = $0 (500MB). Demo-week EKS burst ≈ $12
one-time (control plane $0.10/h × 72h ≈ $7.20 + 2× t3.small).

**AI APIs**:
| Provider | Free tier | Paid | Role |
|---|---|---|---|
| Deepgram | $200 credit, no card | ~$0.0043/min | primary ASR |
| OpenAI Whisper | — | $0.006/min | ASR fallback (budget-gated) |
| DeepL | 500,000 chars/mo | $25/M chars | primary translation |
| Google Translate | 500,000 chars/mo | $20/M chars | translation fallback |
| ElevenLabs | ~10,000 chars/mo | $5/mo / 30K chars | primary TTS |
| Piper (self-host) | unlimited | $0 | TTS fallback + bulk |

**Per-demo** (5-min EN → ES+HI): ASR 10 min ≈ $0.06 on Whisper, $0 on Deepgram
credits · translate ~15K chars = $0 (free tier) · TTS 15K chars = $0 with
Piper. **Total ≈ $0.00–0.80 depending on route.**

**Semester budget**: Weeks 1–8 $0 (local + free tiers) · Weeks 9–12 $10–20
(one t3.micro + ECR) · demo week +$12 optional EKS · **total ≤ $50, $50+
buffer**. AWS Budgets alerts at $40/$70.

## 12. Work breakdown (superseded)

> **Superseded by `docs/plan.md`.** The table below is kept only as the historical target-design breakdown — do not build from it. MVP scope, dates, owners, and acceptance criteria live in `plan.md` (3 services, 1 work queue, 1 dashboard, dumb fallback).

<details>
<summary>Historical target-design breakdown (not current)</summary>

| Wk | Milestone | Owner | Deliverables | Acceptance criteria |
|---|---|---|---|---|
| 1 | Repo bootstrap, ADRs, IaC skeleton | A | repo layout, terraform plan for SQS/S3/ECR, CI skeleton | `ci.yaml` green on empty PR |
| 2 | Gateway + session store | B | `/v1/sessions` CRUD, upload URL, WS skeleton | contract tests pass; OpenAPI published |
| 3 | Slicer | B | ffmpeg smart/fixed chunking, manifest writes | 5-min clip → 38±2 chunks, boundaries ≤1.5s from silence |
| 4 | Queues + ASR worker + AI GW v0 | A+B | SQS wiring, Deepgram via GW, retries | 1 chunk E2E (no TTS) < 1s p50 |
| 5 | Translate + TTS workers (ElevenLabs + Piper sidecar) | B | both workers, fallback path | dubbed 1-min clip plays; Piper fallback fires on forced 500s |
| 6 | Assembler + HLS output | A | timeline alignment, atempo, playlists | drift ≤ 250 ms on 5-min sample |
| 7 | KEDA autoscaling + ArgoCD GitOps | A | ScaledObjects, load test harness, ArgoCD Application | k6 burst: replicas 1→12, backlog returns <0 in 60s; `argocd app sync` deploys |
| 8 | Observability | C+B | OTel spans all stages, 4 Grafana dashboards, alert rules | E2E trace visible per chunk; SLO alert fires on injected breach |
| 9 | Resilience: CB, DLQ redrive, chaos suite + auto-remediation | A | CB states, runbook, chaos workflows, Alertmanager→Job webhooks | provider blackout demo: 0 dropped chunks; auto-redrive verified |
| 10 | FinOps metering + cosign hardening | A+C | price table, cost panels, budget guard, image signing | cost/min panel matches golden-file within 5%; `cosign verify` gates deploy |
| 11 | Web UI + load/chaos polish (+ optional Go ws-relay) | C (+B) | uploader, live player, status page | 50-session load: SLO p95 < 3s, zero DLQ |
| 12 | Demo week: EKS burst (optional), freeze, rehearsal | all | demo script, video backup, README final | 5 (+1 self-healing) demo scenarios pass live, twice |

</details>

**Risk register (target, historical)**: (1) Deepgram free-credit exhaustion →
whisper.cpp evaluated in wk 9; (2) t3.micro saturation → cap at 20 sessions; (3) ElevenLabs exhaustion →
Piper default in `values-dev`. Current risks live in `plan.md`.

## 13. Out of scope (v1)

Voice cloning (documented as future work — slots in as an ElevenLabs
`voice_id` parameter through the same AI GW), speaker diarization, video
lip-sync, multi-node K8s, real WebRTC SFU (WebSocket is sufficient for the
SLO), multi-region.

## 14. References

- Deepgram STT latency & endpoints: developers.deepgram.com (streaming <300 ms; pre-recorded not real-time-safe)
- KEDA SQS scaler & ScaledObject spec: keda.sh
- SQS DLQ & redrive: AWS docs
- Prometheus histograms & `histogram_quantile`: prometheus.io/docs/practices/histograms
- OTel FastAPI instrumentation: opentelemetry-python-contrib
- ffmpeg `segment`/`silencedetect`: ffmpeg.org

## 15. Decision log (append-only)

| Date | Decision | Author | Impact |
|---|---|---|---|
| 2026-09-18 | k3s over EKS for steady state | A | -$73/mo; EKS only for demo week |
| 2026-09-18 | SQS over MSK | A | -$540/mo; acceptable at project scale |
| 2026-09-18 | 8s chunks over 2s/30s | B | balances API overhead vs latency floor |
| 2026-09-18 | Piper as TTS fallback | B | unlimited free TTS; resilience demo |
| 2026-09-18 | Python mono-stack for v1; Go only as optional ws-relay | B | one base image/linter/test runner; SLO is provider-bound |
| 2026-09-18 | ArgoCD GitOps + auto-remediation Jobs + cosign (target) | A | declarative deploys, closed alert→action loop, supply-chain signing; ~1 week cost |
| 2026-09-18 | MVP cut: single `dub-worker` + 1 work queue (ADR-8) | all | 3 deployments replace 7; stage timers keep histogram data |
| 2026-09-18 | MVP cut: dashboards 5 alerts→1 dashboard 2 alerts; OTel→logs+histograms | C+B | one screen tells the demo story; traces deferred |
| 2026-09-18 | MVP cut: remediation Jobs cut, manual redrive; cosign attempt-only | A | alert → runbook is the MVP path; Trivy enforced |
| 2026-09-18 | MVP cut: ArgoCD→stretch wk10; Go relay→future; pytest 80%→70% | all | `helm upgrade` is the MVP deploy; buffer preserved |
