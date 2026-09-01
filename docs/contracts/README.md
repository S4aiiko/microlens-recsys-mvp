# Shared Contracts

Status: Phase 2D runtime-aligned boundary.

These files are the machine-readable boundary between Data, Backend, Frontend,
Worker, Model and Recommendation work packages. They define capabilities; they
do not claim the corresponding business logic is implemented.

- All timestamps are RFC 3339 UTC and aggregation windows are half-open `[from,to)`.
- IDs are opaque strings unless a schema states otherwise.
- PostgreSQL is authoritative; Redis contains only reconstructable cache/queue state.
- Canonical `impression` is a server/persistence-only event. Client single and batch
  requests accept exactly `click`, `like`, `not_interested`, `dwell`, `revisit`, `share`.
- `offline` content always outranks promotions and cached/model candidates.
- A page request has a unique `request_id`; pagination keeps the same signed/opaque snapshot.
- `systems_only` event-derived model artifacts are non-comparable and cannot be activated.
- The 7B fixture is isolated under a `microlens-7b-*` namespace and may never reset the default stack.
- `openapi.json` is the browser/public contract only. The host-published listener is
  `http://api:8000`, rejects `/internal/*`, and exposes no activation path, publish
  token scheme or internal publishing symbol to the generated web SDK.
- `internal-openapi.json` is a separate, self-contained contract for the
  Compose-internal, non-host-published `http://api:8001` listener. It contains only
  model activation and its independent publish token. Phase 2D implements this
  listener on the Compose-only port 8001; the web generator must never consume it.
- `openapi.json` and `internal-openapi.json` are generated from the mounted runtime
  applications by `make generate-contracts`; `make check-contract-drift` fails on
  runtime drift. The browser client is generated only from `openapi.json`.
- `/api/feeds/{feed_type}` authenticates every supported browser role but returns a
  structured 501 with `x-implementation-phase: phase_4_deferred`. Phase 2D does not
  claim the recommendation-feed acceptance loop is complete.

Install the declared `.[dev,api,data]` extras for the complete suite. A base host without
those extras may explicitly skip dependency-backed checks. Run `make test` for JSON
Schema/OpenAPI/PyYAML Compose/ignore-boundary contract checks.

For a local stack, run `make init-env` once before Compose commands. It preserves
existing `.env` assignments, atomically generates only missing secrets/settings,
sets mode 0600, and never prints secret values. Then `make up` or `make smoke-all`
uses that ignored local file. Do not commit `.env`.
