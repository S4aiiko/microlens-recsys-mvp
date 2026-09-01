# Shared Contracts

Status: Frozen Phase 1 boundary after Main Orchestrator static and ARM64 review.

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
  model activation and its independent publish token. Phase 1 does not implement the
  activation handler/listener, and the web generator must never consume this file.

Install the declared `.[dev,api]` extras for the complete suite. A base host without
those extras may explicitly skip dependency-backed checks. Run `make test` for JSON
Schema/OpenAPI/PyYAML Compose/ignore-boundary contract checks.
