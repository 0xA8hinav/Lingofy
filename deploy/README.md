# Deploying Lingofy

Two paths, same images. Everything below is verified in CI (`ci.yaml`).

## 1. Local / dev (k3d, kind, or plain Docker)

```bash
# one command, same images Helm ships:
docker compose -f deploy/dev/docker-compose.yml up --build
# UI: http://localhost:8080   health: /healthz   metrics: /metrics
```

## 2. AWS demo (single EC2 + k3s, EBS volumes)

```bash
# 0. one-time infra (ECR x3, S3 x2, SQS x2 + DLQ, IAM):
cd deploy/terraform && terraform init && terraform apply

# 1. push images (tags must match values-demo.yaml):
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <ACC>.dkr.ecr.ap-south-1.amazonaws.com
docker build -f services/dub-worker/Dockerfile -t <REG>/lingofy/lingofy-full:demo .
docker build -f services/assembler/Dockerfile -t <REG>/lingofy/lingofy-assembler:demo .
docker push <REG>/lingofy/lingofy-full:demo
docker push <REG>/lingofy/lingofy-assembler:demo

# 2. paid-provider keys NEVER go in git — one Secret:
kubectl create secret generic lingofy-providers \
  --from-file=gcloud.json=/path/to/gcloud-stt.json

# 3. deploy:
helm upgrade --install lingofy deploy/helm/lingofy -f deploy/helm/lingofy/values-demo.yaml

# 4. smoke:
kubectl port-forward svc/lingofy-gateway 8080:80 &
curl localhost:8080/healthz   # {"ok":true}
curl localhost:8080/metrics   # prometheus gauges
```

## What lives where

| Piece | Path | Notes |
|---|---|---|
| Full image (gateway + inline dub) | `services/dub-worker/Dockerfile` | ~5GB (torch). Gateway reuses it; slim split is future work |
| Assembler image | `services/assembler/Dockerfile` | stdlib + ffmpeg only |
| Compose (dev parity) | `deploy/dev/docker-compose.yml` | bind-mount-free: named volumes |
| Helm chart | `deploy/helm/lingofy/` | 2 Deployments + PVCs + HPA + optional Ingress |
| KEDA (SQS phase) | `deploy/keda/scaledobject.yaml` | `enabled: false` until the app speaks SQS |
| Terraform (~200 lines) | `deploy/terraform/` | ECR×3, S3×2, SQS×2+DLQ, IAM node role |
| Dashboard + 2 alerts | `deploy/observability/` | scrapes `/metrics` (annotated for Prometheus) |
| CI | `.github/workflows/ci.yaml` | ruff → pytest → build → trivy → helm lint/template |

## Honest scope notes (see docs/plan.md §"What was cut")

- **Queues are local files today** (`data/queue/`). The SQS resources exist in
  Terraform and the KEDA ScaledObject is written, but the app data-plane
  switch (boto3 SQS/S3 behind `STORAGE_BACKEND=s3`) is the next work item.
  Single-node k3s + EBS is the supported topology until then.
- **Gateway replicas stay 1** (in-memory sessions). HPA scales 1→4 on CPU;
  real scale-out needs session store extraction (future scope).
- **Spend**: free-tier t3 path stays ≈$0; paid STT is opt-in and metered in
  `data/cost_ledger.jsonl` + `/metrics`.
