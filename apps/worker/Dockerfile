# Validated on linux/arm64 with Docker Desktop 4.88.1.
FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY requirements-api.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-api.lock

COPY apps ./apps
COPY recsys ./recsys
COPY configs ./configs

EXPOSE 8081
CMD ["python", "-m", "apps.worker.app"]
