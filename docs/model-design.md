# Two-stage recommendation model design

## Scope and trust boundary

The offline model is a CPU-first PyTorch DSSM/two-tower recall stage followed by a
DeepFM reranker. Random and train-popularity baselines use the same frozen candidate
policy. The trainer reads one explicit immutable `data_version` plus the exact data
manifest SHA-256; `latest`, symlinks, missing files, codec drift, size drift and checksum
drift fail before training. The API only loads a standalone `ModelBundle`; it never
imports the trainer or executes a checkpoint.

Phase 3 used MicroLens, pytorch-fm and RecStudio as design references only. No candidate
source was copied or executed. The exact review commits, licenses and CPU/ARM64 decisions
are recorded in `.assignment-flow/references/phase-3-model.md`.

## Data, leakage and evaluation

Interactions are strict per-user time splits. The character n-gram hash-TFIDF title
encoder fits document frequencies and fitted-item membership from items that actually
occur in `train` interactions only. It then transforms the complete catalog, including
validation/test-only items. User title features merge only train-history titles.
Popularity, user activity, novelty, negative-sampling weights and time decay are also
train-only. Tests prove that transforming validation/test-only titles does not change
the encoder checksum and that a deliberately leaky fit does.

Validation chooses epochs and triggers early stopping. Test is evaluated once after both
best validation checkpoints are restored. The candidate policy is the full catalog minus
the user's train-seen items, with deterministic score/item-ID tie breaking. Reported
metrics are macro user averages of Recall@K, NDCG@K and HitRate@K. A tie-aware AUC helper
is tested but is not presented as a ranking KPI. Random and popularity baselines use the
identical eligibility policy.

`quality_evaluation` inputs additionally require strictly ordered frozen train,
validation and later test windows. Every interaction timestamp and declared holdout
count is cross-checked. `systems_only` and any `non_comparable` input can produce only an
`EVALUATED`, activation-ineligible artifact with consumption evidence and no quality
metrics or improvement claim.

## DSSM recall

The user tower concatenates a learned user ID embedding with an EmbeddingBag summary of
train-history title features. The item tower concatenates item ID and item-title
EmbeddingBag features. Independent MLPs map them to normalized vectors; dot product is
the recall score. Sampled softmax uses explicit negatives and optional half-life weights.

Supported negative strategies are:

- `uniform`: deterministic sampling over eligible train items.
- `popularity_aware`: without-replacement sampling weighted by train popularity.
- `train_only_hard`: highest title-cosine candidates inside a deterministic bounded
  train-only pool (random coverage plus popular items), then train popularity and item ID.

All strategies exclude the positive, user train-seen items and every validation/test-only
item. Candidate export stores item ID, rank and score per user.

## DeepFM ranking

DeepFM training examples are built from real DSSM candidate output plus each train
positive; candidate negatives and optional online negative signals are filtered back to
train-interaction items, so validation/test items are never assigned false negative labels.
Its categorical fields are user, item and recall source. Its frozen dense
feature vector is DSSM score, train-history/title similarity, normalized train
popularity, novelty, train user activity and the train time-decay weight. Optional
negative online signals are consumed only when their immutable split is `train`.
DeepFM combines linear terms, pairwise factorization-machine interactions and an MLP.

## Reproducibility, artifacts and serving

Both stages are config-driven and record seed, per-epoch loss/validation metric, best
epoch, stop reason and resume origin. Checkpoints are atomic safe JSON tensor documents
containing current/best model states, Adam state and early-stop state; pickle is not
accepted. Resume requires exact data version, data manifest checksum and resolved config
checksum. Large artifacts and checkpoint directories remain ignored.

The immutable artifact directory includes resolved config, title encoder, DSSM and
DeepFM checkpoints, item embeddings/IDs, DSSM candidates, stage history, metrics JSON and
CSV, Badcase CSV, manifest and a standalone bundle. Every listed artifact has size,
SHA-256, shape and dtype. Existing content-addressed directories are fully revalidated.
The bundle embeds safe tensor states and enforces a 16 MiB staging limit, exact manifest
and config checksums, strict title/ID/popularity/metric types, ID/table shapes, policy
invariants and a real DSSM+DeepFM forward smoke on load. Non-finite numeric values,
boolean-as-number values, duplicate/empty IDs and semantic config coercions fail before
any tensor state is decoded or either model is constructed.

The manifest authenticates a `model_identity` document containing immutable data and
config lineage, seed, title encoder, ordered user/item IDs, train popularity, DSSM and
DeepFM safe states, metrics, and stage-execution checksums. The stage checksum covers
both stages' best epoch, stop reason, full history and resume origin. `model_version` is
derived from the canonical complete identity, not accepted from bundle input. The loader
recomputes every serving-payload checksum and the version before state decoding; changing
model weights, map order, popularity, metrics or evidence therefore fails closed.

The synchronous CLI and worker call the same `recsys.models.entrypoint.train_model`.
Worker output explicitly says `published=false` and `activated=false`. `modelctl publish`
first performs local bundle validation/load smoke and rejects non-READY,
non-comparable, ineligible or systems-only versions. It then sends exactly
`expected_current_version` and `manifest_checksum` with the dedicated publish token to
the Compose-only activation API. The shared integration layer owns the preceding
transactional READY registration (IR-3A-READY-REGISTRATION); any registration or CAS,
token, checksum, schema or staging failure must leave the current active model unchanged.

## Config and experiment matrix

`smoke-a.json` and `smoke-b.json` are two meaningful DSSM and DeepFM hyperparameter
groups and produce distinct official smoke bundles. `ablation-title-off.json` is the
title negative control: it zeros title inputs in both DSSM towers and the DeepFM title
similarity field while retaining the exact same tensor shape. `experiment-matrix.json`
freezes one-variable comparisons for
uniform/popularity-aware/train-only-hard sampling, decay off/on with 3-day and 14-day
half-lives, title off/on, and the two capacity/optimizer groups. Phase 7A materializes
and runs this matrix on full official data; smoke results are never extrapolated or
reported as full-data quality.

Badcase output distinguishes recall misses, rerank misses, short/long history, sparse
title information, popularity bias and duplicate-title diversity. Empty categories are
kept as legitimate results rather than fabricated rows.

## Commands

Train synchronously with explicit immutable inputs:

```bash
python -m recsys.cli.train_model \
  --processed-root /data/processed \
  --data-version DATA_VERSION \
  --data-manifest-checksum DATA_MANIFEST_SHA256 \
  --config configs/models/smoke-a.json \
  --output-root /artifacts/models
```

Validate and atomically activate an already registered READY version:

```bash
python -m recsys.cli.modelctl validate \
  --bundle /artifacts/models/MODEL_VERSION/bundle.json \
  --manifest-checksum MODEL_MANIFEST_SHA256

PUBLISH_TOKEN='from ignored .env' python -m recsys.cli.modelctl publish \
  --bundle /artifacts/models/MODEL_VERSION/bundle.json \
  --manifest-checksum MODEL_MANIFEST_SHA256 \
  --expected-current-version CURRENT_MODEL_VERSION \
  --internal-api-url http://api:8001
```

The first activation omits `--expected-current-version`, which serializes JSON `null`.
Never place the token on a command line or in a tracked file.
