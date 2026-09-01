# CPU-only PyTorch runtime validated on linux/arm64. The base digest matches
# the API and worker images so Python and OS behavior stay reproducible.
FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY requirements-model.lock requirements-data.lock ./
RUN pip install --no-cache-dir --require-hashes \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    --requirement requirements-model.lock \
    --requirement requirements-data.lock

COPY recsys ./recsys
COPY configs ./configs

CMD ["python", "-c", "import torch; print({'torch': torch.__version__, 'device': 'cpu', 'cuda_available': torch.cuda.is_available()})"]
