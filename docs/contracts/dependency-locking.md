# Dependency Locking Strategy

Status: Phase 1 ARM64 verification complete; initial locks frozen on 2026-09-01.

- Frontend: `package.json` uses exact direct versions and `package-lock.json` is the
  reproducible install source. `@hey-api/openapi-ts` is approved only as a locked
  dev dependency and reads only the public browser contract
  `docs/contracts/openapi.json` locally. It must never read
  `docs/contracts/internal-openapi.json` or emit internal activation/publish-token
  symbols into the web client.
- Python: `pyproject.toml` keeps bounded compatibility declarations;
  `requirements-api.lock` and `requirements-dev.lock` freeze exact transitive
  versions and hashes. The API image installs with `pip --require-hashes` from the
  ARM64-specific API lock. PyTorch is not yet a dependency and must be independently
  frozen only after the model-stage CPU/ARM64 build.
- Container images: the validated Python 3.12 slim and Node 22 Alpine base-image
  digests are pinned in their Dockerfiles.
- PostgreSQL/Redis: the validated PostgreSQL 16 Alpine and Redis 7 Alpine digests
  are pinned in `compose.yaml`. Both services passed ARM64 health and named-volume
  container-recreation persistence checks.
- Docker Desktop 4.88.1 / Compose 5.4.0 has a local Buildx gRPC session defect for
  `docker compose build`; equivalent direct builds of all three Compose-named project
  images passed and `docker compose up --no-build --wait` brought all five services
  healthy. The integration phase must provide and retest a stable one-command wrapper.
- GitHub Actions: Anchore SBOM and GitHub dependency review remain reference-only;
  no workflow is created in Phase 1.
- Every lock or image pin change must update the later OSS inventory/SBOM and pass
  license, vulnerability, secret and data-exclusion checks.
