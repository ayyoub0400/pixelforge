# pixelforge developer commands.
#
# `make up` and `make load` need nothing but Docker. `make test` runs the suite
# in a container too, so a Python toolchain on the host is optional.

SHELL := /bin/sh
COMPOSE ?= docker compose
PYTHON ?= python3
BASE_URL ?= http://localhost:8000

# Load generator defaults; override on the command line, e.g.
#   make load RATE=50 DURATION=60 CONCURRENCY=32
RATE ?= 5
DURATION ?= 30
CONCURRENCY ?= 8

.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps build test test-local test-cov lint fmt \
        fixtures deps load load-burst smoke chaos-latency chaos-errors \
        chaos-readiness chaos-reset clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ---------------------------------------------------------------- local stack

up: ## Build and start LocalStack, the API and the worker
	$(COMPOSE) up --build -d
	@echo "api      http://localhost:8000  (docs at /docs)"
	@echo "worker   http://localhost:9090/metrics"
	@echo "aws      http://localhost:4566  (LocalStack)"

down: ## Stop everything and delete volumes
	$(COMPOSE) down --volumes --remove-orphans

restart: ## Recreate the api and worker containers
	$(COMPOSE) up --build -d --force-recreate api worker

logs: ## Follow the api and worker logs
	$(COMPOSE) logs -f api worker

ps: ## Show container status
	$(COMPOSE) ps

build: ## Build both service images without starting anything
	docker build -f docker/Dockerfile.api -t pixelforge-api:dev .
	docker build -f docker/Dockerfile.worker -t pixelforge-worker:dev .

## ----------------------------------------------------------------- test suite

test: ## Run the test suite in a container (no host Python needed)
	docker run --rm -v "$(CURDIR)":/src -w /src python:3.12-slim sh -c \
	  "pip install --quiet --no-cache-dir -r requirements/dev.txt && python -m pytest"

test-local: ## Run the test suite with the host Python
	$(PYTHON) -m pytest

test-cov: ## Run the suite with a coverage report
	$(PYTHON) -m pytest --cov --cov-report=term-missing

lint: ## Lint with ruff (if installed)
	$(PYTHON) -m ruff check api worker shared loadgen tests
	$(PYTHON) -m ruff format --check api worker shared loadgen tests

fmt: ## Format with ruff
	$(PYTHON) -m ruff format api worker shared loadgen tests

fixtures: ## Regenerate the test fixtures
	$(PYTHON) fixtures/generate_fixtures.py

deps: ## Recompile the pinned requirement closures from the .in files
	@for name in base api worker loadgen dev; do \
	  uv pip compile --universal --python-version 3.12 --no-header \
	    -o requirements/$$name.txt requirements/$$name.in; \
	done
	@echo "re-add the header comments if you regenerate by hand"

## ------------------------------------------------------------------ load test

load: ## Generate load against the running stack
	$(COMPOSE) --profile tools run --rm loadgen \
	  --rate $(RATE) --duration $(DURATION) --concurrency $(CONCURRENCY) --poll

load-burst: ## Generate a burst large enough to drive worker autoscaling
	$(COMPOSE) --profile tools run --rm loadgen \
	  --rate 120 --duration 60 --concurrency 64

smoke: ## Upload one image and poll it to completion
	@job_id=$$(curl -sf -X POST "$(BASE_URL)/api/v1/jobs" \
	    -F "file=@fixtures/landscape.jpg;type=image/jpeg" \
	  | sed -E 's/.*"job_id":"([^"]+)".*/\1/'); \
	echo "job_id=$$job_id"; \
	for i in $$(seq 1 60); do \
	  body=$$(curl -sf "$(BASE_URL)/api/v1/jobs/$$job_id"); \
	  case "$$body" in \
	    *'"COMPLETE"'*) echo "$$body"; exit 0 ;; \
	    *'"FAILED"'*)  echo "$$body"; exit 1 ;; \
	  esac; \
	  sleep 1; \
	done; \
	echo "timed out waiting for $$job_id"; exit 1

## ---------------------------------------------------------------------- chaos

chaos-latency: ## Add 2s of latency to /api/v1 requests
	curl -sf -X POST "$(BASE_URL)/admin/chaos" \
	  -H 'Content-Type: application/json' -d '{"latency_ms": 2000}'; echo

chaos-errors: ## Fail half of all /api/v1 requests
	curl -sf -X POST "$(BASE_URL)/admin/chaos" \
	  -H 'Content-Type: application/json' -d '{"error_rate": 0.5}'; echo

chaos-readiness: ## Make /readyz report 503 (pod leaves the Service endpoints)
	curl -sf -X POST "$(BASE_URL)/admin/chaos" \
	  -H 'Content-Type: application/json' -d '{"fail_readiness": true}'; echo

chaos-reset: ## Clear every chaos setting
	curl -sf -X POST "$(BASE_URL)/admin/chaos" \
	  -H 'Content-Type: application/json' \
	  -d '{"fail_readiness": false, "latency_ms": 0, "error_rate": 0}'; echo

## --------------------------------------------------------------------- tidy up

clean: ## Remove caches and coverage artefacts
	rm -rf .pytest_cache .coverage htmlcov coverage.xml .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
