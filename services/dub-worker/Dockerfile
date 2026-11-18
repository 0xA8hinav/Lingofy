# Lingofy full-stack image: gateway + dub-worker in one (the gateway dubs
# inline via services.dub_worker, so splitting the image buys nothing yet).
# The slim services/gateway/Dockerfile is kept for the future split.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=0
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/dub-worker/requirements.txt /tmp/requirements.txt
COPY services/gateway/requirements.txt /tmp/requirements-gw.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-gw.txt
# Pre-bake Piper voices so the image dubs with $0 and no runtime download.
COPY assets/voices/ /app/assets/voices/
COPY services/ /app/services/
COPY scripts/ /app/scripts/
# Gemma/Argos packages download on first use into the image layer cache dir.
ENV ARGOS_PACKAGES_DIR=/app/data/argos
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD curl -sf http://localhost:8080/healthz || exit 1
# Overridden per deployment: gateway runs uvicorn; worker runs the poll loop.
CMD ["uvicorn", "services.gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
