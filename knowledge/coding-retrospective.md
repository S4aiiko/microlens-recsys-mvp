# Coding Retrospective

## 2026-09-02

### Task: Phase 2E/2F search runtime integration

- **Bug** (`verification gap`): `search_reindex` correctly returned `replayed=true`, but
  `SqlAlchemySearchIndexRegistry.mark_active()` still advanced `generation` and rewrote
  activation timestamps on every exact replay.
- **Root cause**: unit coverage checked the replay result and final active index, but did not
  compare durable state before and after the idempotent path.
- **Rule**: every `replayed`, `duplicate`, or idempotent success path must assert that business
  output is stable and that `generation`/`version`, timestamps, watermarks, receipts, and audit
  rows remain unchanged unless the contract explicitly defines reconciliation writes.
- **Next check**: run the operation twice against the real backing service and query the
  authoritative database before and after the second call.
- **Triage**: `append-retrospective`; project-specific reliability rule, no skill change needed.

### Task: Phase 3 atomic model activation

- **Bug** (`state bug`, `verification gap`): the A -> B activation passed SQLite tests but
  failed on PostgreSQL with `uq_model_versions_single_active`; SQLAlchemy flushed the new
  ACTIVE row before archiving the old one, creating a transient uniqueness violation.
- **Root cause**: the test asserted only the committed end state and did not verify SQL flush
  order against the production partial unique index.
- **Rule**: for state transitions protected by a partial unique index, explicitly flush the
  state that releases uniqueness before setting the replacement state, within one transaction.
  Do not treat SQLite final-state coverage as a substitute for the production database gate.
- **Next check**: capture the ORM flush sequence in a unit regression, then run a real
  PostgreSQL A -> B transition, stale CAS, final-state query, and restart restore.
- **Triage**: `append-retrospective`; durable project reliability rule, no skill change needed.

### Task: Phase 4 production recommendation trace

- **Bug** (`verification gap`): a focused test proved that the selected child logger was
  enabled and capturable, but the rebuilt Uvicorn container still emitted no
  `recommendation_page` record until the service used the actually configured
  `uvicorn.error` logger.
- **Root cause**: the regression asserted Python logger state in-process rather than the
  production logging configuration's final handler output.
- **Rule**: acceptance for required production logs must rebuild the production image,
  trigger the real route, inspect the container log sink, parse the record and scan it for
  forbidden secret fields; `isEnabledFor()` and test capture alone are insufficient.
- **Next check**: include one real request ID in the handoff and locate the matching JSON
  record in production-format logs before marking observability PASS.
- **Triage**: `append-retrospective`; project-specific verification rule, no skill change needed.

### Task: Phase 4 bounded cache outage gate

- **Bug** (`verification gap`): Redis outage requests split across several controlled tool
  calls exceeded the five-second process fallback TTL and correctly logged `miss`, which
  initially looked like a cache fallback defect.
- **Root cause**: the fault-injection procedure did not include its own elapsed time in the
  bounded-cache contract and therefore tested after the fallback entry had expired.
- **Rule**: TTL-sensitive fault injection must atomically orchestrate prime, fault, request
  and recovery, guarantee recovery with `finally`, and distinguish an expired fallback
  miss from an in-window fallback failure without changing production TTL to fit the test.
- **Next check**: record the fallback TTL and run the complete outage sequence inside one
  orchestrator process; require the live trace to say `process_fallback_hit`.
- **Triage**: `append-retrospective`; reusable project reliability rule, no skill change needed.

## 2026-09-03

### Task: Phase 5C scheduled operations JSON boundary

- **Bug** (`type/API misuse`, `test gap`): `OperationJobCreateRequest` used model-wide Pydantic
  `strict=True`, so valid browser JSON UUID and timezone-aware datetime strings were rejected with
  422 even though object-level service tests passed.
- **Root cause**: strictness was applied to the transport model rather than the fields that must
  reject coercion, and no authenticated HTTP JSON regression exercised the generated-client shape.
- **Rule**: for JSON request models, validate UUID/datetime through real HTTP JSON and use
  field-level strict types for numeric/boolean coercion boundaries; do not infer JSON compatibility
  from direct Python model construction or service tests.
- **Next check**: submit one OpenAPI-shaped payload through auth + CSRF, then assert valid UUID/
  timezone strings succeed while string/boolean numerics and naive datetimes return 422 with no
  durable job created.
- **Triage**: `append-retrospective`; project API-boundary rule, no skill change needed.
