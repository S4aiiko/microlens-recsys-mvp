# Validated on linux/arm64 with Docker Desktop 4.88.1; keep the tag for
# readability and the digest for reproducibility.
FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY requirements-api.lock requirements-data.lock ./
RUN pip install --no-cache-dir --require-hashes \
    --requirement requirements-api.lock \
    --requirement requirements-data.lock
COPY apps ./apps
COPY recsys ./recsys
COPY configs ./configs
COPY scripts/platform_commands.py ./scripts/platform_commands.py

EXPOSE 8000 8001
CMD ["python", "-m", "apps.api.app.server"]
