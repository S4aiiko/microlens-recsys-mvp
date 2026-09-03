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

### Task: Phase 6 event-training export contract

- **Bug** (`logic bug`, `verification gap`): live official items with
  `metadata_status=complete_snapshot_unusable_as_of_feature` were exported as
  `missing_item_metadata`, then rejected by `validate_event_export` because their IDs were
  known to the immutable base dataset.
- **Root cause**: the exporter treated a feature-leakage restriction on likes/views as if the
  item itself were unknown/incomplete; the live official metadata status was not included in
  the end-to-end event export contract test.
- **Rule**: distinguish item identity/metadata completeness from feature eligibility. Every
  exporter rejection reason must be replayed through the downstream validator using the exact
  base `known_item_ids` set before accepting the contract.
- **Next check**: after generating an official Feed event, run export and
  `build-training-data` in the production container; require accepted rows for the documented
  complete snapshot status and validator acceptance, plus a regression for genuinely missing
  items.
- **Triage**: `append-retrospective`; durable project data-contract rule, no skill change needed.

### Task: Phase 7A Fix exact-SHA cursor CI

- **Bug** (`edge case`, `test gap`): the cursor tamper regression changed only the final unpadded
  Base64URL character. When that change affected unused low bits, strict decoding still produced the
  original HMAC bytes and the exact-SHA CI nondeterministically failed to raise `CursorError`.
- **Root cause**: the test assumed every textual Base64URL mutation changes decoded bytes, while the
  production decoder accepted multiple noncanonical spellings of the same byte sequence.
- **Rule**: opaque signed-token decoders must either require canonical re-encoding or explicitly define
  representation aliases as valid. Tamper tests must include the unused-bit alias case and must not
  rely on randomly replacing the final Base64URL character.
- **Next check**: construct two different unpadded Base64URL strings that decode to identical bytes,
  assert the decoder rejects the alias, then run the exact CI test under multiple generated payloads.
- **Triage**: `append-retrospective`; project security and test-determinism rule, no skill change needed.

### Task: Phase 7A isolated runtime preflight

- **Bug** (`file/path mistake`, `verification gap`): the first shared launcher probe passed an absolute
  matrix path to a resolver whose established safety contract accepts only repository-relative paths;
  the next probe treated Docker Desktop's inactive kernel tunnel devices and loopback-only IPv6 routes
  as usable external networking even under `--network=none`.
- **Root cause**: the new launcher/runtime assertions were written from assumed path and Linux network
  shapes before exercising the existing resolver and the target Docker Desktop kernel view.
- **Rule**: a security preflight must reuse each existing parser's accepted input shape and distinguish
  operational interfaces/routes from inactive kernel devices. Prove isolation with active interfaces,
  non-loopback default routes and fixed launcher argv, not raw interface-name or route-row counts.
- **Next check**: before freezing an attested image, render the exact tokenized launcher command, run the
  shared no-data preflight on the target runtime and require an empty output bind plus unchanged service
  snapshots.
- **Triage**: `append-retrospective`; durable project runtime-verification rule, no skill change needed.

### Task: Phase 7A commit identity and namespace claim

- **Bug** (`trust-boundary gap`, `concurrency bug`): the first provenance checksum excluded the host
  launcher and Makefile, while namespace initialization used check-then-replace and allowed concurrent
  callers to overwrite an accepted marker.
- **Root cause**: content attestation was scoped to container application directories rather than every
  executable/build input, and atomic replacement was mistaken for exclusive initialization.
- **Rule**: formal provenance must byte-compare the complete current executable boundary with the
  requested commit tree before any build/render/run. One-time namespaces require exclusive creation
  (`O_EXCL`) plus file/directory fsync; atomic rename alone does not establish a single winner.
- **Next check**: mutate a host launcher and copied image source independently, race compatible and
  incompatible claimants behind a barrier, and inject write/file-fsync/directory-fsync failures while
  requiring no Docker/output side effect or surviving partial marker.
- **Triage**: `append-retrospective`; durable project provenance/concurrency rule, no skill change needed.

### Task: Phase 7A RSS capture replay

- **Bug** (`state bug`, `test gap`): `load_capture_metadata()` validated a raw four-field capture but
  returned the enriched object containing `container_envelope`; `probe_candidate_rss()` then validated
  it again as raw input, so the real CLI always failed while direct-dictionary unit tests passed.
- **Root cause**: tests covered the parser and probe independently but skipped the production composition
  from file load through probe execution.
- **Rule**: when validation derives fields, keep the raw-input and normalized-output types explicit and
  test the exact CLI composition path; do not pass normalized data back through a strict raw-schema gate.
- **Next check**: load capture JSON from a real file, pass that return value into the probe, and run the
  exact immutable-image Docker argv before accepting machine-readable evidence.
- **Triage**: `append-retrospective`; durable project validation-boundary rule, no skill change needed.

### Task: Phase 7A external execution evidence grammar

- **Bug** (`trust-boundary gap`, `verification gap`): RSS capture validation counted required safe Docker
  tokens but accepted extra conflicting flags and `readonly=false`, then derived an inaccurately safe
  `container_envelope`; the documented checksum prerequisite also entered the ignored virtualenv before
  the launcher's clean-tree gate.
- **Root cause**: presence checks were treated as an execution grammar, and the formal workflow was reviewed
  one command at a time instead of from its first executable prerequisite.
- **Rule**: machine evidence for an external command must validate the complete ordered argv and exact option
  grammar, rejecting every unrecognized, duplicate, alias or conflicting token. Every prerequisite that
  imports project code must share the same isolated clean-tree bootstrap as the final action.
- **Next check**: mutate the captured argv with conflicting split/equal options and writable/extra mounts,
  then run the documented workflow from its first command with ignored startup-hook sentinels installed.
- **Triage**: `append-retrospective`; durable project provenance rule, no skill change needed.
