PYTHON ?= python3
DOCKER_COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help doctor init-env data-inspect data-download smoke-all up up-core ps logs test down \
	full-data train-full export-events build-training-data train-async worker-run-once \
	job-status cache-stats publish prepare-7b-fixture covers generate-client check-client-drift \
	migrate seed generate-contracts check-contract-drift test-api test-integration

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
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) up -d --build --wait db redis api worker web

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

train-full:
	@$(PYTHON) scripts/foundation.py placeholder train-full

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
	@$(PYTHON) scripts/foundation.py placeholder train-async

worker-run-once:
	@$(DOCKER_COMPOSE) version >/dev/null
	$(DOCKER_COMPOSE) exec -T worker python -m apps.worker.app run-once

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
