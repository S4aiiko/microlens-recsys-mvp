# Validated on linux/arm64 with Docker Desktop 4.88.1; keep the tag for
# readability and the digest for reproducibility.
FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
# Keep this block byte-for-byte aligned with docker/model.Dockerfile so the
# validated linux/arm64 CPU PyTorch layer is reusable across images.
COPY requirements-model.lock requirements-data.lock ./
RUN pip install --no-cache-dir --require-hashes \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    --requirement requirements-model.lock \
    --requirement requirements-data.lock

COPY requirements-api.lock requirements-search.lock requirements-analytics.lock ./
RUN pip install --no-cache-dir --require-hashes \
    --requirement requirements-api.lock \
    --requirement requirements-search.lock \
    --requirement requirements-analytics.lock
COPY apps ./apps
COPY recsys ./recsys
COPY configs ./configs
COPY scripts/platform_commands.py ./scripts/platform_commands.py

EXPOSE 8000 8001
CMD ["python", "-m", "apps.api.app.server"]
