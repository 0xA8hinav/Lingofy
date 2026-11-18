FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# stdlib-only service: no pip install step on purpose.
COPY services/assembler/ /app/services/assembler/
COPY services/__init__.py /app/services/__init__.py
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import services.assembler.assemble" || exit 1
CMD ["python3", "-m", "services.assembler.assemble"]
