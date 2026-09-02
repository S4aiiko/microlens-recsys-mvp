# Online recommendation design

## Scope and authority

Phase 4 composes the active immutable `ModelBundle`, PostgreSQL catalog/profile/event
state, the existing versioned Redis cache, and the existing snapshot/event persistence
service. PostgreSQL remains authoritative for user permission, active model version,
item online state, profile version, operations and promotions. Redis contains only
reconstructable natural recall payloads; it never contains a final page or grants
delivery permission.

The implementation is in `apps/api/app/feeds/`. It does not commit transactions. The
router factory owns one commit after snapshot/page persistence succeeds and rolls back
on cursor, authority or snapshot conflicts. `SnapshotService` writes the immutable
snapshot and, for each delivered page, one unique recommendation request plus actual
exposures and deterministic canonical impressions in the same database transaction.

## Recall and ranking

Natural recall has five explicit sources:

- `dssm`: active bundle DSSM catalog recall for a mapped `source_user_id`;
- `item_item_cf`: cosine co-occurrence over immutable train user histories;
- `profile_title`: positive profile token overlap with item titles;
- `popular`: train popularity plus bounded online likes/views popularity;
- `explore`: deterministic seed-based novelty and low-popularity preference.

`personalized` combines all available personalized sources with popular/explore
fallback. `popular` uses popularity, and `explore` uses deterministic exploration.
Scores are min-max normalized within each source, duplicate item IDs are merged, and a
bounded agreement bonus preserves multi-source evidence. Source and reason survive into
the snapshot.

DeepFM uses categorical user/item fields and source index `0`, because the frozen Phase
3 model has `source_count=1`. Its six dense fields remain in the training order:

1. DSSM recall score, explicitly retained even when another source is the merge winner;
2. active title encoder history/item cosine;
3. log-normalized bundle train popularity;
4. train novelty as `1 - popularity`;
5. log-normalized current profile activity;
6. neutral online decay weight `1.0`.

Unknown users/items or model exceptions keep deterministic merged-score ranking and add
an explicit fallback reason. No exception turns a failed model output into authority.

## Filtering, diversity and promotions

Current PostgreSQL online state is checked after every cache result, during cursor page
selection, and again inside `SnapshotService.record_page`. Viewed impressions and
`not_interested` events are removed when a new snapshot is built. Old snapshots are
never reranked; only newly offline items may be skipped during delivery.

`title_topic` is explicitly named `derived_title_topic:<bucket>` from the active
train-fitted title encoder. Without a model it is labeled `derived_title_hash:<digest>`;
neither form claims author or tag provenance. Topic deduplication and MMR are independent
configuration switches.

MMR follows section 5.8 exactly. DeepFM scores are min-max normalized inside the
post-filter snapshot candidate set; equal scores normalize to `1.0`. Similarity is
`max(0, cosine)`, the first maximum similarity is zero, and
`lambda * relevance - (1 - lambda) * maximum_similarity` uses default lambda `0.75`.
Ties use original rank then item ID. A missing title vector uses the original score and
records `missing_title_vector_original_score`. Active scoped promotions are inserted
only after natural diversity. Promotion priority/position is deterministic, while the
operations query and final page checks ensure offline always wins.

## Cursor and snapshot semantics

The HMAC cursor binds version, snapshot ID, user ID, feed type, response offset,
snapshot scan offset and expiry. It strictly rejects type coercion, booleans as integers,
oversized offsets, malformed payloads, tampering, cross-user/feed use and expiry.

Response `offset` and internal `scan_offset` are separate. If snapshot position 2 goes
offline after page one, page two can scan past it, return later immutable candidates at
contiguous response positions, and advance both offsets without repeating an item.
Every page gets a new request ID while snapshot ID and model version remain unchanged.

## Cache and trace

The cache key includes user/feed/config identity plus PostgreSQL profile version, active
model version, successful operation count and permission generation. A hit is followed
by a fresh online catalog and behavior query. A syntactically valid but schema-invalid
payload is not coerced: the exact versioned key is invalidated and loaded once from the
authoritative path. Redis failure uses the existing bounded process fallback and then
the loader.

Each delivered page logs one JSON record with snapshot/request/user/feed/model, source
counts, filter counts, cache status, latency, fallback reasons and all MMR selection
steps. Cache hits/replays do not advance profile/operation generations or other durable
state.

## Integration boundary

The owned router is a factory and is not mounted yet because `apps/api/app/main.py`, the
checked-in OpenAPI document and generated client are integration-owned. The service also
accepts an injected immutable `ItemItemIndex`; current shared contracts do not expose a
checksum-verified active-data train-history artifact to the API process. These two
integration requests are recorded in the Phase 4 handoff. Until both are completed and
their acceptance tests pass, Phase 4 is ready for integration rather than fully PASS.
