# File Ownership After Phase 1

Shared contracts freeze only after Main Orchestrator review. Cross-owner changes
must be submitted as an Integration Request; feature agents must not edit shared
files directly.

| Owner | Exclusive scope |
|---|---|
| Project Integration Agent | `compose.yaml`, `Makefile`, `.env.example`, root dependency/lock files, `apps/worker/`, generated OpenAPI client, shared routes/styles, integration tests, public OSS/NOTICE/SBOM files |
| Data Agent | `recsys/data/`, `configs/data/`, `tests/data/`, data schema documentation |
| Backend Core Agent | API database models, Alembic chain, authentication, events, operations, model registry, admin query layer |
| Frontend Foundation / Integration Agent | `apps/web` root configuration, global route/layout, generated client and shared components |
| Model Agent | `recsys/models/`, `recsys/serving/`, model CLI/config/tests |
| Recommendation Service Agent | API feed/ranking/profile adapters and assigned integration tests; no migration edits |
| User Web Agent | `apps/web/src/features/auth`, `feed`, `profile` |
| Admin Web Agent | `apps/web/src/features/admin` |

Each work package exclusively owns:

- `.assignment-flow/references/<package>.md`
- `.assignment-flow/handoffs/<package>.md`

Only Project Integration Agent serially updates public OSS inventory,
`THIRD_PARTY_NOTICES.md`, `licenses/` and SBOM after approval.
