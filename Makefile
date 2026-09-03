PYTHON ?= python3
DOCKER_COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help doctor init-env data-inspect data-download smoke-all up up-core ps logs test down \
	full-data train-full train-sync export-events build-training-data train-async worker-run-once \
	job-status cache-stats publish prepare-7b-fixture covers generate-client check-client-drift \
	migrate seed generate-contracts check-contract-drift test-api test-integration \
	scheduler-run-once search-reindex register-model phase7a-resolve phase7a-checksum phase7a-build \
	phase7a-run phase7a-preflight phase7a-render

help:
	@$(PYTHON) scripts/foundation.py help

doctor:
	@$(PYTHON) scripts/foundation.py doctor

init-env:
	@$(PYTHON) scripts/init_env.py

data-inspect:
	@PYTHONPATH=. $(PYTHON) -m recsys.data.cli inspect --raw-dir "$${MICROLENS_DATA_DIR:-dataset}"

# Explicitly blocked: download authorization and destination must be reviewed at invocation time.
data-download:
	@$(PYTHON) scripts/foundation.py placeholder data-download

covers:
	@$(PYTHON) scripts/foundation.py placeholder covers

smoke-all:
	@PYTHONPATH=. $(PYTHON) scripts/smoke_all.py \
		--run-id "$${SMOKE_RUN_ID:?SMOKE_RUN_ID is required}" \
		--docker-compose "$(DOCKER_COMPOSE)"

up:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) up -d --build --wait

# Core interactive stack excludes the worker. Redis remains a required Phase 2D
# runtime dependency; degradation tests stop it only after API startup.
up-core:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) up -d --build db redis api web

ps:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) ps

logs:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) logs --follow

down:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) down

test:
	@$(PYTHON) -m unittest discover -s tests/contract -v

test-api:
	@PYTHONPATH=. $(PYTHON) -m pytest -q tests/api

test-integration:
	@PYTHONPATH=. $(PYTHON) -m pytest -q tests/integration

generate-client:
	@npm --prefix apps/web run generate-client

check-client-drift:
	@npm --prefix apps/web run check-client-drift

generate-contracts:
	@PYTHONPATH=. $(PYTHON) scripts/generate_contracts.py

check-contract-drift:
	@PYTHONPATH=. $(PYTHON) scripts/generate_contracts.py --check

migrate:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T api python -m scripts.platform_commands migrate

seed:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T api python -m scripts.platform_commands seed

full-data:
	@PYTHONPATH=. $(PYTHON) -m recsys.data.cli build-official \
		--config configs/data/full.yaml \
		--raw-dir "$${MICROLENS_DATA_DIR:-dataset}" \
		--output-root "$${PROCESSED_DATA_DIR:-data/processed}"

train-full: train-sync

phase7a-resolve:
	@PYTHONPATH=. $(PYTHON) -m recsys.experiments.phase7a_cli resolve \
		--matrix configs/models/experiment-matrix.json \
		--repo-root .

phase7a-checksum:
	@$(PYTHON) -I -S scripts/phase7a_launcher.py checksum --repo-root .

phase7a-build:
	@$(PYTHON) -I -S scripts/phase7a_launcher.py build --repo-root .

phase7a-run:
	@$(PYTHON) -I -S scripts/phase7a_launcher.py run --repo-root .

phase7a-preflight:
	@$(PYTHON) -I -S scripts/phase7a_launcher.py preflight --repo-root .

phase7a-render:
	@$(PYTHON) -I -S scripts/phase7a_launcher.py run --repo-root . --render

train-sync:
	@test -n "$(DATA_VERSION)" || (echo "DATA_VERSION is required" >&2; exit 2)
	@test "$(DATA_VERSION)" != "latest" || (echo "DATA_VERSION must not be latest" >&2; exit 2)
	@test -n "$(DATA_MANIFEST_CHECKSUM)" || (echo "DATA_MANIFEST_CHECKSUM is required" >&2; exit 2)
	@test -n "$(MODEL_CONFIG)" || (echo "MODEL_CONFIG is required" >&2; exit 2)
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T worker python -m recsys.cli.train_model \
		--processed-root /artifacts/processed \
		--data-version "$(DATA_VERSION)" \
		--data-manifest-checksum "$(DATA_MANIFEST_CHECKSUM)" \
		--config "$(MODEL_CONFIG)" \
		--output-root /artifacts/models

export-events:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T api python -m apps.api.app.cli.export_training_events \
		--watermark-name "$${WATERMARK_NAME:-online-events}"

build-training-data:
	@test -n "$(EXPORT)" || (echo "EXPORT is required" >&2; exit 2)
	@test -n "$(BASE_DATA_VERSION)" || (echo "BASE_DATA_VERSION is required" >&2; exit 2)
	@test -n "$(PURPOSE)" || (echo "PURPOSE is required" >&2; exit 2)
	@PYTHONPATH=. $(PYTHON) -m recsys.data.cli build-training-data \
		--base-data-version "$(BASE_DATA_VERSION)" \
		--processed-root "$${PROCESSED_DATA_DIR:-data/processed}" \
		--event-export "$(EXPORT)" \
		--mapping-config "$${MAPPING_CONFIG:-configs/data/event-mapping-systems-v1.yaml}" \
		--purpose "$(PURPOSE)"

train-async:
	@test -n "$(IDEMPOTENCY_KEY)" || (echo "IDEMPOTENCY_KEY is required" >&2; exit 2)
	@test -n "$(DATA_VERSION)" || (echo "DATA_VERSION is required" >&2; exit 2)
	@test "$(DATA_VERSION)" != "latest" || (echo "DATA_VERSION must not be latest" >&2; exit 2)
	@test -n "$(DATA_MANIFEST_CHECKSUM)" || (echo "DATA_MANIFEST_CHECKSUM is required" >&2; exit 2)
	@test -n "$(CONFIG_CHECKSUM)" || (echo "CONFIG_CHECKSUM is required" >&2; exit 2)
	@test -n "$(PURPOSE)" || (echo "PURPOSE is required" >&2; exit 2)
	@test -n "$(COMPARABILITY)" || (echo "COMPARABILITY is required" >&2; exit 2)
	@test -n "$(ACTIVATION_ELIGIBLE)" || (echo "ACTIVATION_ELIGIBLE is required" >&2; exit 2)
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T api python -m apps.api.app.cli.enqueue_training \
		--idempotency-key "$(IDEMPOTENCY_KEY)" \
		--data-version "$(DATA_VERSION)" \
		--data-manifest-checksum "$(DATA_MANIFEST_CHECKSUM)" \
		--config-checksum "$(CONFIG_CHECKSUM)" \
		--purpose "$(PURPOSE)" \
		--evaluation-comparability "$(COMPARABILITY)" \
		--activation-eligible "$(ACTIVATION_ELIGIBLE)"

register-model:
	@test -n "$(MODEL_BUNDLE)" || (echo "MODEL_BUNDLE is required" >&2; exit 2)
	@test -n "$(MANIFEST_CHECKSUM)" || (echo "MANIFEST_CHECKSUM is required" >&2; exit 2)
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T api python -m apps.api.app.cli.register_model \
		--artifact-uri "$(MODEL_BUNDLE)" \
		--manifest-checksum "$(MANIFEST_CHECKSUM)"

worker-run-once:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T worker python -m apps.worker.app run-once

scheduler-run-once:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T scheduler python -m apps.worker.scheduler run-once

search-reindex:
	@test -n "$(INDEX_VERSION)" || (echo "INDEX_VERSION is required" >&2; exit 2)
	@test -n "$(SOURCE_VERSION)" || (echo "SOURCE_VERSION is required" >&2; exit 2)
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T scheduler python -m apps.api.app.cli.search_reindex \
		--index-version "$(INDEX_VERSION)" \
		--source-version "$(SOURCE_VERSION)" \
		$(if $(EXPECTED_CURRENT_INDEX),--expected-current-index "$(EXPECTED_CURRENT_INDEX)",)

job-status:
	@$(PYTHON) scripts/foundation.py placeholder job-status

cache-stats:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T api python -m apps.api.app.cli.cache_stats

publish:
	@$(PYTHON) scripts/foundation.py placeholder publish

prepare-7b-fixture:
	@test -n "$(FIXTURE_ID)" || (echo "FIXTURE_ID is required" >&2; exit 2)
	@PYTHONPATH=. $(PYTHON) scripts/prepare_7b_fixture.py \
		--fixture-id "$(FIXTURE_ID)" \
		--model-bundle-a "$(MODEL_BUNDLE_A)" \
		--model-bundle-a-sha256 "$(MODEL_BUNDLE_A_SHA256)" \
		--model-bundle-b "$(MODEL_BUNDLE_B)" \
		--model-bundle-b-sha256 "$(MODEL_BUNDLE_B_SHA256)"
