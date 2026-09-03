# Phase 7A Experiment Contract

Phase 7A uses `configs/models/experiment-matrix.json` as its public, immutable experiment
contract. Resolve it before allocating training resources:

```bash
make PYTHON=.venv/bin/python phase7a-resolve
```

The resolver rejects unknown matrix fields and override paths. Every model candidate differs from
the full-data base config at one allowlisted path; `dssm` and `deepfm` are each treated as one named
hyperparameter group. All candidates train with validation-only early stopping. Selection uses the
configured validation metric and deterministic experiment-ID tie break. Test rows are deserialized once
after selection is frozen; the selected model is finalized once, and its immutable cohort is reused for
Random/Popularity baselines, Badcase analysis and the six predeclared serving ablations.

Build the formal worker image with the reviewed source identity, then resolve and freeze its exact OCI
digest. A mutable tag is never a valid runner input:

```bash
GIT_REVISION="$(GIT_NO_REPLACE_OBJECTS=1 git rev-parse HEAD)"
PHASE7A_SOURCE_CHECKSUM="$(make --no-print-directory \
  PYTHON=.venv/bin/python phase7a-checksum)"
GIT_REVISION="$GIT_REVISION" \
PHASE7A_SOURCE_CHECKSUM="$PHASE7A_SOURCE_CHECKSUM" \
PHASE7A_BUILD_TAG=<reviewed-worker-tag> \
make PYTHON=.venv/bin/python phase7a-build
```

The generated build command starts with `docker buildx build --pull=false --load` and fixes the BuildKit
execution envelope with three repeated arguments: `--resource memory=5g`,
`--resource cpu-period=100000` and `--resource cpu-quota=400000`. Docker 29.7.2 exposes this
`--resource` syntax.
The checksum, build, render, preflight and run Make entries all use the same stdlib-only isolated launcher
startup (`python -I -S`). The checksum entry derives HEAD with replacement objects disabled and validates
the complete clean commit tree before printing its source checksum. Ignored virtualenv `.pth`,
`sitecustomize` and `usercustomize` hooks therefore cannot execute before the gate, and a dirty checkout is
never used for checksum calculation or an actual build.

Before formal execution, use `phase7a-preflight` with an existing empty probe directory. It invokes the
same direct `docker run` argv builder but only verifies the baked/recomputed source identity and runtime
envelope; it does not open processed data or write output. Formal execution requires a different run root
that does not exist yet. Build, render, preflight and run require an entirely clean worktree: no staged
change, unstaged tracked change or non-ignored untracked path may exist before Docker invocation or output
creation. Git plumbing runs with replacement objects disabled. The external clean-tree controls explicitly
include `.dockerignore`, `pyproject.toml`, `.github/workflows/ci.yml` and this public contract, while the
separate source manifest remains scoped to the image/runtime executable boundary:

```bash
PHASE7A_IMAGE=<name@sha256:64-lowercase-hex> \
PHASE7A_SOURCE_CHECKSUM=<reviewed-source-sha256> \
PHASE7A_PROCESSED_ROOT=<absolute-processed-root> \
PHASE7A_RUN_ROOT=<absolute-fresh-run-root> \
DATA_VERSION=<immutable-data-version> \
DATA_MANIFEST_CHECKSUM=<sha256> \
GIT_REVISION=<40-character-git-sha> \
RUN_ID=<new-run-id> \
make PYTHON=.venv/bin/python phase7a-run
```

The launcher never resolves Compose or `.env`. It applies `--pull=never`, `--network=none`, a read-only
root filesystem, fixed 5 GiB memory/no-swap, 4 CPU and 512 PID limits, a restricted 256 MiB `/tmp`, one
read-only processed-data bind and one read-write fresh namespace bind. The namespace marker binds Git,
image, source, matrix, base config and data identities. The marker is an exclusive claim: the absent host
root is created once, and every compatible or incompatible contender/reuse is refused without rewriting
it. Before that claim, the runner validates the immutable data version and actual manifest checksum by
reading only `manifest.json`; it does not deserialize split rows. `run.json` records the identities plus
the command, seed, environment, elapsed time, validation metrics, epoch histories, best epochs, stop
reasons, frozen selection and selected-model final metrics/checksums. `serving-ablations.json` records the
same frozen test cohort checksum and the six source/topic/MMR ablations. Artifact integrity checks may
hash test-file bytes before selection, but no test rows are decoded or used until selection is frozen.

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
or full user-by-catalog matrix is created. The checked 64-bit CPython 3.12 budget for 50,000 users,
Top-200 and 19,220 items uses a 128-byte upper allowance per retained slot, 4 KiB per user container
set, one 128-user float32 catalog-score batch and a 2 GiB reserve for model, data and runtime state.
The resulting `3,642,124,288`-byte upper bound is below the fixed 5 GiB (`5,368,709,120` bytes)
no-swap cgroup cap with `1,726,584,832` bytes (about 1.61 GiB) headroom. The slot allowance covers
the two item-reference lists, score-map entry/index storage and one Python float without charging
shared catalog strings more than once.

The tracked bounded probe can reproduce the retained candidate list, nested score-map and reranked-list
shapes for 10,000 users x Top-200 (2,000,000 slots):

```bash
docker run --rm --pull=never --network=none --read-only \
  --memory=512m --memory-swap=512m --cpus=4 --pids-limit=128 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --mount type=bind,src="$PWD/scripts/phase7a_candidate_rss_probe.py",dst=/probe.py,readonly \
  --mount type=bind,src="$PWD/docs/evidence/phase7a-candidate-rss-capture.json",dst=/probe-capture.json,readonly \
  python@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc \
  python /probe.py --capture-metadata /probe-capture.json
```

`docs/evidence/phase7a-candidate-rss-capture.json` is the raw capture record. It binds the explicit UTC
capture time, immutable image reference, local image ID, probe-script SHA-256 and complete tokenized
Docker argv. The containerized probe verifies the SHA-256 of its executing `__file__` bytes, validates
the rest of that record against one complete ordered argv grammar and derives the machine-readable
`container_envelope` in
`docs/evidence/phase7a-candidate-rss-probe.json`; the evidence JSON, rather than this prose, is
authoritative for the executed image and limits.

The saved CPython 3.12.14 sample records raw Linux `ru_maxrss` values of `23,556` and `180,356` KiB:
a `160,563,200`-byte delta, or `80.2816` bytes per slot. RSS samples vary with allocator/runtime state;
each run emits its own raw values and algorithm. This is focused structure/RSS evidence for the 128-byte
slot allowance, not a full-data model run or a claim about formal Phase 7A peak RSS.

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
