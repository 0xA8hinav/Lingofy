FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# --- gateway: light HTTP edge, no ML deps ---
FROM base AS gateway
WORKDIR /app
COPY services/gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/gateway/ /app/services/gateway/
COPY services/__init__.py /app/services/__init__.py
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD curl -sf http://localhost:8080/healthz || exit 1
CMD ["uvicorn", "services.gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
