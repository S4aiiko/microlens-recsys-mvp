# Validated on linux/arm64 with Docker Desktop 4.88.1.
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

ARG GIT_REVISION=unattested
ARG PHASE7A_SOURCE_CHECKSUM=unattested

COPY apps ./apps
COPY recsys ./recsys
COPY configs ./configs
COPY Makefile requirements-model.lock requirements-data.lock requirements-api.lock \
    requirements-search.lock requirements-analytics.lock ./
COPY scripts/phase7a_launcher.py ./scripts/phase7a_launcher.py
RUN python -m recsys.experiments.source_identity attest \
    --repo-root /workspace \
    --git-revision "${GIT_REVISION}" \
    --expected-source-checksum "${PHASE7A_SOURCE_CHECKSUM}" \
    --output /usr/local/share/microlens/phase7a-source.json

LABEL org.opencontainers.image.revision="${GIT_REVISION}" \
    io.microlens.phase7a.source-checksum="${PHASE7A_SOURCE_CHECKSUM}" \
    io.microlens.phase7a.attestation="/usr/local/share/microlens/phase7a-source.json"

EXPOSE 8081
CMD ["python", "-m", "apps.worker.app"]
