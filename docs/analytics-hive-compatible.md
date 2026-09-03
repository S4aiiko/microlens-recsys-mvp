# Hive-compatible analytics export

This package writes local, partitioned Parquet that follows Hive-style directory naming.
It has **not** connected to or been validated against a real Hive metastore or Hive runtime.
The supported local readers are the exact locked PyArrow runtime and, after a separate
dependency gate, the optional DuckDB adapter.

## Window and source boundary

Every export has an explicit UTC half-open window `[from_utc,to_utc)`. The PostgreSQL adapter
captures `max(events.id)` before reading rows, then binds event, exposure and aggregate queries
to `(previous_event_sequence_exclusive,event_sequence_cutoff_inclusive]`. Exposures use their
same-transaction canonical impression's `events.id` as the monotonic sequence boundary.

Client timestamps never decide inclusion. Events use `server_timestamp`; exposures use
`exposed_at`. A row committed after the captured cutoff is a late arrival for this immutable
revision. It is not inserted into an already-published directory: a follow-up export must name
the previous manifest checksum as its parent and advance the source watermark.

## Layout

```text
analytics-<identity-hash>/
  manifest.json
  manifest.sha256
  events/dt=YYYY-MM-DD/event_type=<type>/part-00000.parquet
  exposures/dt=YYYY-MM-DD/feed_type=<type>/part-00000.parquet
```

Only frozen event/feed enum values can form directory names. The partition columns are encoded
in the path and are intentionally omitted from each physical Parquet file. Hive-compatible
readers reconstruct them from the path.

## Atomic and idempotent publication

Files, manifest and checksum sidecar are written and fsynced below a same-filesystem temporary
directory. Publication is one atomic rename, followed by parent-directory fsync. A repeated
identity reuses a completed directory only after validating every declared path, byte size,
SHA-256, Parquet physical schema, row count, aggregate count and absence of unlisted Parquet.
Traversal, symlinked roots/files, modified inputs and incompatible existing output fail closed.

The manifest records the UTC window, event-sequence watermark, late-event policy, parent
manifest checksum, PostgreSQL grouped counts, file descriptors, schema fingerprints and exact
writer settings. `manifest_payload_checksum` covers the canonical body; `manifest.sha256`
covers the complete canonical manifest bytes.

## Schema evolution

Within schema major `1`, existing field name/order/logical type/nullability and partition fields
are immutable. A minor version may append nullable columns only. Dropping, reordering, renaming,
type/nullability changes, or partition changes require a new major and an explicit migration.
`validate_additive_evolution` enforces this rule independently of the writer.

## Reconciliation

`PostgreSQLAnalyticsSource.collect()` obtains grouped counts through independent PostgreSQL
`GROUP BY` queries under the same window/watermark boundary. Export validation compares those
counts with the published partition descriptors. `reconcile_with_pyarrow()` then reads the same
Parquet dataset through PyArrow Hive partition discovery and compares all seven event types and
three feed types with the PostgreSQL counts.

`DuckDBCountReader` is a narrow optional adapter. The integration dependency is pinned as
`duckdb==1.5.5`; its Linux ARM64 runtime and the real Parquet reconciliation path were validated.
The adapter remains opt-in and should only be enabled with the exact lockfile and platform
compatibility checks used by the analytics test suite.

## DDL templates

Templates are under `configs/analytics/hive/`. Replace only the `${...}` placeholders with
trusted database/table/location values. Validate `manifest.json` and `manifest.sha256` before
registering partitions. `MSCK REPAIR TABLE` is left commented because no real Hive execution is
part of this project scope.
