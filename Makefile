PYTHON ?= python3
DOCKER_COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help doctor data-inspect data-download smoke-all up up-core ps logs test down \
	full-data train-full export-events build-training-data train-async worker-run-once \
	job-status cache-stats publish prepare-7b-fixture covers generate-client check-client-drift

help:
	@$(PYTHON) scripts/foundation.py help

doctor:
	@$(PYTHON) scripts/foundation.py doctor

data-inspect:
	@$(PYTHON) scripts/foundation.py data-inspect

# Explicitly blocked: download authorization and destination must be reviewed at invocation time.
data-download:
	@$(PYTHON) scripts/foundation.py placeholder data-download

covers:
	@$(PYTHON) scripts/foundation.py placeholder covers

smoke-all:
	@$(PYTHON) scripts/foundation.py placeholder smoke-all

up:
	@$(PYTHON) scripts/foundation.py require-docker
	$(DOCKER_COMPOSE) up -d

# Deliberately excludes Redis and worker for the later degradation test contract.
up-core:
	@$(PYTHON) scripts/foundation.py require-docker
	$(DOCKER_COMPOSE) up -d db api web

ps:
	@$(PYTHON) scripts/foundation.py require-docker
	$(DOCKER_COMPOSE) ps

logs:
	@$(PYTHON) scripts/foundation.py require-docker
	$(DOCKER_COMPOSE) logs --follow

down:
	@$(PYTHON) scripts/foundation.py require-docker
	$(DOCKER_COMPOSE) down

test:
	@$(PYTHON) -m unittest discover -s tests/contract -v

generate-client:
	@npm --prefix apps/web run generate-client

check-client-drift:
	@npm --prefix apps/web run check-client-drift

full-data:
	@$(PYTHON) scripts/foundation.py placeholder full-data

train-full:
	@$(PYTHON) scripts/foundation.py placeholder train-full

export-events:
	@$(PYTHON) scripts/foundation.py placeholder export-events

build-training-data:
	@$(PYTHON) scripts/foundation.py placeholder build-training-data

train-async:
	@$(PYTHON) scripts/foundation.py placeholder train-async

worker-run-once:
	@$(PYTHON) scripts/foundation.py placeholder worker-run-once

job-status:
	@$(PYTHON) scripts/foundation.py placeholder job-status

cache-stats:
	@$(PYTHON) scripts/foundation.py placeholder cache-stats

publish:
	@$(PYTHON) scripts/foundation.py placeholder publish

prepare-7b-fixture:
	@$(PYTHON) scripts/foundation.py placeholder prepare-7b-fixture
