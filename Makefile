# Developer shortcuts. Run `make help`.
# Recipes start with ">" instead of a tab (see .RECIPEPREFIX), so editors cannot break them.
.RECIPEPREFIX := >
SHELL := /bin/bash
PY := .venv/bin/python
ENV ?= dev

.DEFAULT_GOAL := help
.PHONY: help setup env db-up db-down db-psql db-reset check test-int lock

help: ## show this help
> @grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

setup: ## create .venv and install the package with dev tools
> python3 -m venv .venv
> $(PY) -m pip install --upgrade pip
> $(PY) -m pip install -e ".[dev]"

env: ## generate env/$(ENV).env with random passwords (ENV=dev|staging|production)
> $(PY) -m cabler.envgen $(ENV)

db-up: ## build and start the database of $(ENV)
> $(PY) -m cabler.dbctl up $(ENV)

db-down: ## stop the database of $(ENV) (data is kept)
> $(PY) -m cabler.dbctl down $(ENV)

db-psql: ## open psql inside the database container
> $(PY) -m cabler.dbctl psql $(ENV)

db-reset: ## DELETE the data of $(ENV) and start again
> $(PY) -m cabler.dbctl destroy $(ENV)
> $(PY) -m cabler.dbctl up $(ENV)

check: ## lint, format check, type check, unit tests
> .venv/bin/poe check

test-int: ## database integration tests against $(ENV)
> CABLER_ENV=$(ENV) .venv/bin/poe test-int

lock: ## refresh the pinned dependency list
> mkdir -p requirements
> $(PY) -m pip freeze --exclude-editable > requirements/dev.lock
