# Lingofy — Architecture

> Layered view. The Mermaid data-flow diagram lives in `README.md` §2.
> This doc explains the layers, the failure story, and why each choice was made.
> Full service specs: `docs/TECHNICAL_REFERENCE.md`.

## 1. Layered view (for slides)

```
┌─────────────────────────────────────────────────────────┐
│ Edge: Web UI → FastAPI Gateway (REST + WS)              │
├─────────────────────────────────────────────────────────┤
│ Ingest: Slicer (ffmpeg, 8s silence-snapped chunks) → S3 │
├─────────────────────────────────────────────────────────┤
│ Pipeline: SQS asr-tasks → translate-tasks → tts-tasks   │
│           + shared DLQ + redrive                        │
├─────────────────────────────────────────────────────────┤
│ Workers: ASR (KEDA) │ Translate (KEDA) │ TTS (KEDA)     │
├─────────────────────────────────────────────────────────┤
│ AI Gateway: retry + CB + fallback + metering + budgets  │
├─────────────────────────────────────────────────────────┤
│ Providers: Deepgram │ DeepL │ ElevenLabs │ Piper (self) │
├─────────────────────────────────────────────────────────┤
│ Synth: Assembler (atempo align) → HLS playlists + manifest│
├─────────────────────────────────────────────────────────┤
│ Observability: OTel → Prometheus → Grafana → Alertmanager│
│   + auto-remediation Jobs (scale-bump, redrive, verify) │
├─────────────────────────────────────────────────────────┤
│ Platform: k3s (k3d dev) │ Helm + ArgoCD │ Terraform AWS │
│   CI: ruff/mypy/pytest/trivy/cosign → ECR → GitOps sync │
└─────────────────────────────────────────────────────────┘
```

Test workload happens to be audio. The system under test is the SLO,
the scaling, the failure handling, and the cost control.

## 2. Request lifecycle

File mode: `POST /v1/sessions` → signed S3 URL → upload → Slicer → chunk
messages → ASR → Translate (1→N fan-out per language) → TTS → Assembler →
`playlist.m3u8` per language. Live mode: same pipeline, ingest over
`WS /v1/sessions/{id}/stream`.

Correlation: `chunk_id` in every SQS envelope + OTel trace. One trace spans
all seven steps per chunk.

## 3. Scaling story

- Unit of parallelism = **chunk**, not session. N sessions × 8s chunks →
  uniform queue depth.
- KEDA `ScaledObject` per worker on `ApproximateNumberOfMessages`:
  `queueLength: 5`, `min: 1`, `max: 12` (TTS max 8, Piper sidecar is CPU-bound),
  `cooldownPeriod: 120`, `pollingInterval: 10`.
- Scale-to-zero disabled for demo warmth; documented as prod variant.
- Money-shot panel: replica count overlaid with queue depth.

## 4. Resilience story

AI Gateway is the single egress for all provider calls:

- Retries: 3 attempts, full-jitter backoff, 429/5xx/timeout only.
- Circuit breaker per provider: 5 fails/30s → open 30s → half-open probe →
  2 successes → closed. Metric `lingofy_circuit_state` (0/1/2).
- Fallbacks: ElevenLabs → Piper sidecar, Deepgram → Whisper (budget-gated),
  DeepL → Google Translate.
- Workers hold no provider creds; keys live in AI GW only.
- Queues: visibility timeouts per stage (30–120s), maxReceiveCount 3 → shared
  `lingofy-dlq` (14d retention), console + auto-Job redrive.
- Chaos-tested: worker kill (SQS redelivery), provider blackout (CB + fallback),
  node loss (GitOps redeploy, RTO ≈ 8 min).

Auto-remediation (week 9): Alertmanager webhooks → bounded Jobs:
SLOBurn → scale-bump, DLQNonEmpty → auto-redrive post-recovery,
CircuitOpen → fallback canary verify.

## 5. Observability story

- Tracing: OTel FastAPI auto + manual spans around queue send/receive and
  provider calls; 100% sample dev, 20% load.
- Metrics (`lingofy_` prefix): stage histograms (buckets to 6.4s so p95 sits
  inside a real bucket), queue depth/wait, E2E SLO histogram, tokens/cost
  counters, CB gauge, DLQ counter, drift histogram.
- Dashboards: SLO Overview, Workers & Scaling, AI Providers, FinOps.
- Alerts: SLOBurn (p95>3s 5m, page), StageFailureSpike, DLQNonEmpty,
  CircuitOpen, CostAnomaly.

## 6. Language choice

**Python 3.12 mono-stack for v1.** p95 budget is 3,000 ms, ~2,650 ms of which
is provider network + model time and KEDA queue wait. Python glue overhead is
single-digit ms per stage. One base image, one linter (ruff), one test runner
(pytest), first-class `deepgram-sdk` / `deepl` / `elevenlabs` + LiteLLM.

Optional bounded Go: `ws-relay` (~300 lines, WS ↔ SQS/S3, same envelope) in
weeks 9–10 only if live ingest shakes under k6, plus optional kubebuilder TTL
operator. Design rule: *"Language per component based on where latency
originates — providers, so orchestration is Python for velocity; streaming
relay moves to Go/Rust only when connection count becomes the bottleneck."*

Voice maths needs no DSP code: `atempo` for time-stretch, `silencedetect` for
boundaries, integer-ms drift, Prometheus histograms for percentiles. Real DSP
lives in ffmpeg (C) and TTS models (C++/CUDA).

## 7. Platform capabilities

| Capability | Implementation |
|---|---|
| IaC | Terraform for SQS/S3/ECR/IAM; one `terraform apply` |
| GitOps | ArgoCD Application watching repo; `helm upgrade` only as fallback |
| Self-healing | CB + fallback + auto-remediation Jobs closing alert→action loop |
| Security | Trivy block on CRITICAL/HIGH + cosign keyless sign/verify |
| Observability + FinOps | SLO dashboards, tracing, chaos tests, and cost panels live |

Demo flow: Terraform apply → ArgoCD sync → k6 + KEDA
scale → kill provider (CB/fallback) → SLO + cost wall → auto-remediation
recovery.

## 8. ADRs (summary)

ADR-1 k3s over EKS (-$73/mo). ADR-2 SQS over MSK (-$540/mo). ADR-3
chunk-parallel over session-parallel. ADR-4 AI Gateway separate (workers dumb).
ADR-5 Piper fallback (free/unlimited). ADR-6 8s chunks. ADR-7 HLS output.
Full log: `TECHNICAL_REFERENCE.md` §15.
