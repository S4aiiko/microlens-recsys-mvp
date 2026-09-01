# Data schema and reproducibility

Phase 2A reads the three official MicroLens-50K tabular files and writes an
immutable, content-addressed data directory. Raw and processed data remain
gitignored. The pipeline never downloads video, audio, covers, or media.

## Verified official inputs

The read-only inspection on 2026-09-01 observed the following complete files:

| File | Encoding / delimiter | Logical rows | Bytes | SHA-256 |
|---|---|---:|---:|---|
| `MicroLens-50k_pairs.csv` | ASCII CSV, header | 359,708 | 9,431,093 | `7ff8b91bcc84f5434ac2c5be7d0b7d7730f5e84f79f9648b5ae67a7641f97bbd` |
| `MicroLens-50k_titles.csv` | UTF-8 CSV, header | 19,220 | 2,392,145 | `244aad5380cbbe0fb43458cfcda5ebe820f534384602f80a64dbbcd07dd30e49` |
| `MicroLens-50k_likes_and_views.txt` | ASCII tab-separated, no header | 19,220 | 386,787 | `9031dcd6fd575abc28776b6fe55a9b5a5a6446ff1d25bbb97d0e9437f480dfb2` |

The pairs schema is `user,item,timestamp`, where timestamp is Unix epoch
milliseconds. Titles use `item,title`. Likes/views use
`item,likes_snapshot,views_snapshot`. The scan found 50,000 users, 19,220
interacted items, no exact interaction duplicates, no invalid required values,
and complete pair-to-title-to-snapshot joins. Per-user interaction counts range
from 5 to 218. Source timestamps span
`2020-03-05T03:23:49.552Z` through `2022-09-12T12:02:12.429Z`.

Likes and views have no observation timestamp or historical snapshots. They are
therefore item display metadata only and are explicitly excluded from any
leakage-free offline point-in-time feature set.

## Immutable output

The public codec is `parquet_pyarrow_25_0_1_v1` and requires the exact locked
`pyarrow==25.0.1`. The CLI fails closed on another version. Every table has an
explicit Arrow schema and column order. The writer fixes Parquet 2.6, ZSTD level
3, dictionary encoding off, 65,536-row groups, 1 MiB data pages, data page v1,
statistics on, compliant nested types on, and page index off.

```text
data/processed/<data_version>/
├── manifest.json
├── quality_report.json
├── items.parquet
├── train.parquet
├── validation.parquet
├── test.parquet
├── user_history.parquet
├── train_popularity.parquet
├── title_corpus.parquet
└── event_training_signals.parquet  # derived event version only
```

The version identity includes the three raw checksums, resolved config checksum,
seed, and codec contract. Existing versions are read-only: a checksum mismatch or
content-address collision aborts instead of overwriting files. Artifact paths in
the manifest are relative, and every artifact has size, row count where
applicable, and SHA-256.

Core table columns are:

- interactions: `user_id:string, item_id:string, timestamp:int64_epoch_ms`;
- items: `item_id, title, likes_snapshot:int64, views_snapshot:int64,
  cover_ref:null|string, metadata_status`;
- user history: `user_id, ordered_item_ids:list<string>,
  ordered_timestamps:list<int64>, split_cutoffs:struct`;
- train popularity: `item_id, count:int64, probability:float64,
  time_decayed_count:float64`;
- title corpus: `item_id, normalized_title, item_split_membership:list<string>,
  is_train_item:bool`;
- event training signal: `event_id, user_id, item_id,
  server_timestamp:timestamp[ms,UTC], signal_type, label:int8|null,
  sample_weight:float64, source_export_checksum, split`.

Title normalization is deterministic Unicode NFKC plus whitespace collapsing.
Data processing only marks training-item membership. A later model stage must fit
its text vocabulary/encoder on `is_train_item=true` rows and only transform other
titles.

## Split and leakage rules

Interactions are grouped by user and ordered by `(timestamp,item_id)`. Equal
timestamp rows remain in one split. For a user with at least three distinct
timestamps, the last timestamp group is test, the preceding group is validation,
and earlier groups are train. A user lacking three distinct timestamps or the
configured minimum train history remains train-only. Every evaluation user must
satisfy `max(train) < min(validation) < min(test)`.

Smoke mode chooses users by a SHA-256 rank of `(seed,user_id)` and then applies
the same split; it is a stable official-data sample, not synthetic feed data.

Both negative samplers exclude only the target user's training history:

- uniform samples without replacement from all remaining items;
- popularity-aware samples without replacement using
  `train_count ** popularity_alpha`.

Neither sampler uses validation/test behavior for the exclusion set or sampling
probabilities. Popularity, user history, and time-decay reference time are all
computed from train. Exponential decay uses
`exp(-ln(2) * age_seconds / half_life_seconds)`. `full.yaml` and
`full-no-decay.yaml` provide matched enabled/disabled configurations.

## Event export boundary

`build_training_data` accepts an explicit immutable base version, an export
directory, a versioned mapping config, and one purpose. It rejects `latest`.
The export directory contains `manifest.json` and one same-directory
`events.parquet`; paths outside the directory and symlinks are rejected.

The export manifest requires:

```json
{
  "schema_version": "1.0",
  "export_id": "immutable-export-id",
  "event_id_ordering": "database_sequence",
  "watermark": {"start_exclusive": 0, "end_inclusive": 100},
  "export_cutoff_utc": "2026-09-01T00:00:00Z",
  "events_file": {
    "path": "events.parquet",
    "size_bytes": 0,
    "sha256": "64 lowercase hexadecimal characters",
    "rows": 0
  }
}
```

Each event has `event_sequence_id,event_id,user_id,request_id,item_id,position,
event_type,server_timestamp,duration_ms`. Validation checks file size/checksum,
row count, strictly increasing database sequence within the frozen watermark,
final sequence/cutoff agreement, UUIDs, known items, allowed event types,
duration bounds, and server-time cutoff. Duplicate `event_id`, unknown items,
out-of-order/watermark rows, tampering, and timestamps beyond cutoff fail closed.
This mapping version rejects duplicate `event_id` for the entire export instead
of silently dropping a row; consequently a successful derived manifest records
`deduplicated=0`.

The versioned mapping makes click/like/share/revisit and qualifying dwell positive,
`not_interested` negative, and impression exposure context with no label or sample
weight. Dwell below threshold is rejected; durations above the mapping maximum are
capped before weight calculation. Rows outside frozen purpose windows are isolated
and counted.

- `systems_only` puts validated fixture signals in train and always writes
  `evaluation_comparability=non_comparable` and `activation_eligible=false`.
- `quality_evaluation` requires predeclared server-time half-open windows satisfying
  `train_end <= validation_start < validation_end <= test_start < test_end <=
  export_cutoff`, plus minimum interactions and users in each later window.
  Insufficient holdout raises `NOT_ENOUGH_HOLDOUT` and produces no activatable data
  version.

For quality evaluation, all official/base behavior earlier than the new train
cutoff can be reclassified as training history. The base version's old validation
and test split is never reused after later events enter train. Online future-window
signals never enter `train.parquet`; changing them therefore cannot alter its
checksum.

## Commands

```bash
python -m recsys.data.cli inspect --raw-dir dataset
python -m recsys.data.cli build-official \
  --config configs/data/smoke.yaml --raw-dir dataset \
  --output-root data/processed
python -m recsys.data.cli build-training-data \
  --base-data-version <explicit-version> \
  --processed-root data/processed \
  --event-export artifacts/training_exports/<immutable-export> \
  --mapping-config configs/data/event-mapping-systems-v1.yaml \
  --purpose systems_only
```

The shared `make smoke-all`, `make full-data`, and `make build-training-data`
targets remain integration-owned and must be wired by the Project Integration
Agent; Phase 2A does not modify the shared Makefile.

## Verified locked builds

The final writer verification ran in Linux/ARM64 with Python 3.12 and
hash-locked `pyarrow==25.0.1`. A built wheel was installed and
`python -m recsys.data.cli --help` succeeded outside the source checkout. The
current four Parquet tests passed, covering explicit writer settings, exact
version rejection, official artifact round-trip/byte determinism, and derived
event artifact determinism.

Two independent real smoke builds produced identical manifest bytes and all
eight identical artifact files:

- data version: `microlens50k-5242f9dac31d99db`;
- manifest SHA-256:
  `55bad84f2af6774d9c6dc5a51a930e50b2d6fec0797eed5bdc74bfde799bfcfa`;
- observed build times: 2.216 s and 2.156 s in the accepted container.

The real full build completed in 4.428 s:

- data version: `microlens50k-cd591aacb9147924`;
- manifest SHA-256:
  `305af712968432f9d5f44d67087b38092a3427ae81c3789cc06c5abbff7c4f82`;
- rows: train 259,708; validation 50,000; test 50,000; user history
  50,000; items/title corpus 19,220; train popularity 18,490.

For both modes every declared artifact SHA matched the actual file. The accepted
container deleted generated data in the same run after validation; no raw or
processed data entered the repository.
