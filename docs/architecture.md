# Lingofy — Architecture (MVP)

> MVP view. The Mermaid data-flow diagram lives in `README.md` §2.
> This doc explains the layers, the failure story, and why each choice was made.
> Service details: `docs/TECHNICAL_REFERENCE.md`. Build steps: `docs/plan.md`.
> Anything bigger than below lives in §8 Future Scope.

## 1. Layered view

```
┌─────────────────────────────────────────────────────────┐
│ Edge: Web UI → Gateway (FastAPI REST + chunker inside)  │
├─────────────────────────────────────────────────────────┤
│ Queues: SQS dub-tasks + lingofy-dlq (manual redrive)    │
├─────────────────────────────────────────────────────────┤
│ Worker: dub-worker (KEDA) — ASR→Translate→TTS in one    │
│   process, stage timers, providers.py helper            │
├─────────────────────────────────────────────────────────┤
│ Providers: Deepgram │ DeepL │ ElevenLabs │ Piper baked in│
├─────────────────────────────────────────────────────────┤
│ Synth: Assembler (poll S3, atempo align) → HLS + manifest│
├─────────────────────────────────────────────────────────┤
│ Observability: Prometheus → Grafana (1 dashboard) →     │
│   Alertmanager (2 alerts); JSON logs with chunk_id      │
├─────────────────────────────────────────────────────────┤
│ Platform: k3s (k3d dev) │ Helm upgrade │ Terraform AWS  │
│   CI: ruff/pytest/build/trivy → ECR → helm (kind smoke) │
└─────────────────────────────────────────────────────────┘
```

Test workload happens to be audio. The system under test is the SLO,
the scaling, the failure handling, and the cost control.

## 2. Request lifecycle (MVP)

File mode: `POST /v1/sessions` → upload → gateway chunks (8s, silence-snapped) → S3 + one `dub-tasks` message per chunk → dub-worker runs ASR → Translate (1→N langs) → TTS sequentially → per-lang MP3s to S3 → assembler rebuilds `playlist.m3u8` + `manifest.json` every 10s. Live mode: same path, ingest over `WS /v1/sessions/{id}/stream` (skeleton).

Correlation: `chunk_id` in every SQS message, log line, and metric label. No distributed trace store — logs + histograms carry the story.

## 3. Scaling story (MVP — one queue, one scaler)

- Unit of parallelism = **chunk**, not session. N sessions × 8s chunks → uniform queue depth on `dub-tasks`.
- One KEDA `ScaledObject` on `ApproximateNumberOfMessages`: `queueLength: 5` (1 replica per 5 pending), `min: 1`, `max: 8`, `cooldownPeriod: 120`, `pollingInterval: 10`.
- Scale-to-zero disabled for demo warmth.
- The dashboard panel to watch: replica count overlaid with queue depth.

## 4. Resilience story (MVP — dumb + manual)

Provider helper (`providers.py`, inside the worker — not a separate service):

- Retries: 2 attempts, full-jitter backoff, 429/5xx/timeout only. Timeouts: connect 2s, read 10–15s.
- Dumb breaker per provider: 3 consecutive failures → route to fallback for 60s → retry primary once. No half-open probing (documented upgrade path).
- Fallbacks: ElevenLabs → Piper baked into the worker image (`es_ES-mls_1024-low`, `hi_IN-pratham-medium`); Whisper/Google flags exist but default off.
- Queues: `dub-tasks` visibility 60s, maxReceive 3 → `lingofy-dlq` (4d retention on work queue, 14d on DLQ). Redrive is manual (`aws sqs start-message-move-task`), runbooked.
- Chaos is two manual scripts: `chaos/kill-worker.sh` (`kubectl delete pod` → SQS redelivery, complete manifest, empty DLQ) and `chaos/block-provider.sh` (`iptables` block ElevenLabs → fallback counter rises, zero drops). Node loss → `helm upgrade` redeploy, RTO ≈ 8 min.

## 5. Observability story (MVP — 1 dashboard, 2 alerts, logs)

- Metrics (`lingofy_` prefix, buckets `0.05–6.4`): `stage_duration_seconds{stage}` + `session_e2e_latency_seconds` histograms, `queue_depth`, `dlq_messages_total`, `fallback_total{provider}`, `cost_cents_total`, `drift_ms`.
- Logs: JSON lines with `chunk_id`, `seq`, `stage`, `provider`, `ms` — greppable without a trace backend.
- One dashboard (`dashboard.json`), four panels: E2E p95 vs 3s, per-stage p50/p95, replicas vs queue depth, cost/min.
- Two alerts only: SLOBurn (p95 > 3s for 5m), DLQNonEmpty (any ingress in 10m).

## 6. Language choice (MVP)

**Python 3.12 mono-stack.** p95 budget is 3,000 ms, ~2,650 ms of which is provider network + model time and queue wait. Python glue overhead is single-digit ms per stage. One base image, one linter (ruff), one test runner (pytest), first-class `deepgram-sdk` / `deepl` / `elevenlabs`.

Voice maths needs no DSP code: `atempo` for time-stretch, `silencedetect` for boundaries, integer-ms drift, Prometheus histograms for percentiles. Real DSP lives in ffmpeg (C) and TTS models.

## 7. Platform capabilities (MVP)

| Capability | MVP implementation |
|---|---|
| IaC | Terraform ~200 lines: S3×2, SQS×2+DLQ, ECR×3, IAM; one `terraform apply` |
| Deploy | `helm upgrade --install` in CI; ArgoCD only as timeboxed stretch |
| Self-healing | Dumb breaker + Piper fallback + SQS redelivery; manual DLQ redrive |
| Security | Trivy blocks CRITICAL/HIGH in CI; cosign attempted once, dropped on friction |
| Observability | 1 dashboard, 2 alerts, JSON logs |
| Cost | 1 counter + 1 panel from `prices.yaml` |

Demo flow (five moments): upload → play < 3s → k6 burst 1→8 → block ElevenLabs (Piper takes over) → delete pod (redelivery, empty DLQ) + cost paid-vs-free on one graph.

## 8. Future Scope (not built now)

Separate slicer deployment; worker split per AI stage with stage queues; AI Gateway as standalone service (LiteLLM-style); OTel Collector + Tempo/Jaeger manual traces; dashboards 2–4 and extra alerts; 3-state breaker with half-open probing; per-tenant budgets + anomaly detection; DLQ auto-redrive and remediation Jobs; nightly chaos workflow; ArgoCD primary; cosign enforced; Go WS relay; TTL operator; voice cloning, diarization, lip-sync, multi-node, WebRTC SFU, multi-region. Defense lines for each: `docs/plan.md` §"What was cut".

## 9. ADRs (MVP summary)

ADR-1 k3s over EKS (-$73/mo). ADR-2 2-queue SQS over Kafka (-$540/mo). ADR-3 chunk-parallel. ADR-4 provider helper as worker module (no second caller yet). ADR-5 Piper baked in (free/unlimited). ADR-6 8s chunks. ADR-7 HLS output. ADR-8 3 deployments, not 7. Full log: `TECHNICAL_REFERENCE.md` §15.
