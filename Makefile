SHELL := /bin/bash
PY ?= python3.12
VENV := .venv
BIN := $(VENV)/bin
COMPOSE := docker compose

-include .env
export

TEST_DATABASE_URL ?= postgresql://jobapplier:jobapplier@localhost:5432/jobapplier_test
DATABASE_URL ?= postgresql://jobapplier:jobapplier@localhost:5432/jobapplier

.PHONY: help venv up down migrate seed test test-unit lint fmt smoke e2e deploy clean

help:  ## List targets
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

$(BIN)/activate: requirements-dev.txt packages/common/pyproject.toml services/api/pyproject.toml
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements-dev.txt
	touch $(BIN)/activate

venv: $(BIN)/activate  ## Create/refresh the local virtualenv

up:  ## Start the local stack and apply migrations
	$(COMPOSE) up -d --build --wait postgres elasticmq seaweedfs jaeger api
	$(COMPOSE) run --rm s3-init
	$(MAKE) migrate

down:  ## Stop the local stack (keeps volumes; `docker compose down -v` to wipe)
	$(COMPOSE) down

migrate:  ## Apply Alembic migrations to the local database
	$(COMPOSE) exec -T api alembic -c migrations/alembic.ini upgrade head

seed: ## Insert sample data and emit a traced event
	$(COMPOSE) exec -T api python scripts/seed.py

test: venv  ## Run all tests (needs `make up` for the DB tests)
	REQUIRE_DB=1 TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest

test-unit: venv  ## Run tests that need no running services
	$(BIN)/pytest -m "not db"

lint: venv  ## Lint, format check and type-check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy packages/common/src services/api/src

fmt: venv  ## Auto-format and fix lint
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

smoke: venv  ## Phase 1 check: event lands in Postgres and trace shows in Jaeger
	DATABASE_URL=$(DATABASE_URL) $(BIN)/python scripts/smoke.py

e2e:  ## End-to-end pipeline test in dry_run (Phase 8)
	@echo "e2e arrives with the pipeline (Phase 8)"; exit 1

deploy:  ## cdk deploy (Phase 10)
	@echo "deploy arrives with the AWS infrastructure (Phase 10)"; exit 1

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache
