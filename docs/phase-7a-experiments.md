# Phase 7A Experiment Contract

Phase 7A uses `configs/models/experiment-matrix.json` as its public, immutable experiment
contract. Resolve it before allocating training resources:

```bash
make PYTHON=.venv/bin/python phase7a-resolve
```

The resolver rejects unknown matrix fields and override paths. Every model candidate differs from
the full-data base config at one allowlisted path; `dssm` and `deepfm` are each treated as one named
hyperparameter group. All candidates train with validation-only early stopping. Selection uses the
configured validation metric and deterministic experiment-ID tie break. The selected config is
frozen before exactly one final test evaluation; Random and Popularity baselines are evaluated only
in that final pass.

Run the formal matrix in the worker image with explicit immutable identities:

```bash
make phase7a-run \
  DATA_VERSION=<immutable-data-version> \
  DATA_MANIFEST_CHECKSUM=<sha256> \
  GIT_REVISION=<40-character-git-sha> \
  RUN_ID=<new-run-id>
```

`PHASE7A_OUTPUT_ROOT` defaults to gitignored `output/phase7a`. A run refuses an existing run ID.
`run.json` records the command, git/data/matrix/base/resolved-config checksums, seed, environment,
elapsed time, validation metrics, epoch histories, best epochs, stop reasons, frozen selection and
the sole final-test model/checksums/metrics. `serving-ablations.json` records the fixed all-test-user
cohort checksum and the source/topic/MMR ablations.

Experiment IDs that resolve to the same config checksum reuse the first validation execution;
`execution_reused` and `reused_execution_from` make that explicit without changing selection or
the experiment-ID tie break. Once `run.json` exists, an execution exception is atomically recorded
as `FAILED` with its type, a bounded message, elapsed time and completed validation count before the
exception is re-raised.

Final Random and Popularity evaluation scans the full catalog excluding each user's train-seen
items but retains only `max(K)` candidates for one user at a time. Its memory is therefore bounded
by one seen set plus `max(K)`, rather than all-user full-catalog rankings.

Training negatives do not construct a per-row catalog list. Uniform sampling maps sampled eligible
ranks through precomputed item indices. Popularity-aware sampling uses one train-only CDF, at most
32 rejection draws per requested negative, and one deterministic bounded-memory catalog fallback
only for pathological excluded-weight cases.

Final DSSM recall is computed once and retained as top-200 item lists plus score maps for DeepFM.
`dssm_candidates.json` is streamed one user's rows at a time, so no 10,000,000-row Python document
or full user-by-catalog matrix is created. The checked 50,000-user/19,220-item budget uses a lower
bound of 24 bytes per retained slot and a deliberately conservative upper bound of 512 bytes per
slot, 8 KiB per user, one float32 catalog-score batch, and 512 MiB fixed reserve. This yields a
240,000,000-byte lower bound and an upper bound below the 8 GiB container limit.

Activity segments are fixed and shared by model, baseline and serving-ablation metrics:

- `cold_start`: 1-2 train interactions
- `active`: 3-9 train interactions
- `highly_active`: 10 or more train interactions

The serving ablation adapter is read-only and does not connect to PostgreSQL or Redis. It reuses the
production retrieval, per-source normalization/merge, DeepFM ranking, derived-title-topic dedup and
MMR primitives. Its fixed contract uses all test users, a 200-item serving candidate pool, train-seen
exclusion and K values 5/10/20. DSSM uses the selected bundle, item-item CF uses the complete user
train history, and profile-title preferences are deterministic train-history title-token frequencies.
The saved cohort checksum covers these source rules, topic limit and MMR lambda. Recommendation-side
user bucketing, A/B assignment and multi-version traffic remain deferred; they are not part of this
runner.
