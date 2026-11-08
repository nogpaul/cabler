# Developer shortcuts. Run `make help`.
# Recipes start with ">" instead of a tab (see .RECIPEPREFIX), so editors cannot break them.
.RECIPEPREFIX := >
SHELL := /bin/bash
PY := .venv/bin/python
ENV ?= dev

.DEFAULT_GOAL := help
.PHONY: help setup env db-up db-down db-psql db-reset migrate migrate-status sources register updates fetch load diff apply validate review check test-int lock

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

migrate: ## apply pending database migrations to $(ENV)
> $(PY) -m cabler.migrate up $(ENV)

migrate-status: ## show which migrations are applied on $(ENV)
> $(PY) -m cabler.migrate status $(ENV)

sources: ## list the data sources defined in config/sources
> $(PY) -m cabler.pipeline list

register: ## write sources and licences to the database of $(ENV)
> $(PY) -m cabler.pipeline register $(ENV)

updates: ## ask the publishers whether any source changed, without downloading ($(ENV))
> $(PY) -m cabler.pipeline check $(ENV)

fetch: ## download sources into snapshots: make fetch SOURCES=osm_hamburg [YES=1] ($(ENV))
> $(PY) -m cabler.pipeline fetch $(ENV) $(SOURCES) $(if $(YES),--yes,)

load: ## load fetched snapshots into the raw schema: make load SOURCES=osm_hamburg ($(ENV))
> $(PY) -m cabler.pipeline load $(ENV) $(SOURCES)

diff: ## compare loaded snapshots with their predecessors: make diff SOURCES=osm_hamburg ($(ENV))
> $(PY) -m cabler.pipeline diff $(ENV) $(SOURCES)

apply: ## apply diffed snapshots to the staging state: make apply SOURCES=osm_hamburg ($(ENV))
> $(PY) -m cabler.pipeline apply $(ENV) $(SOURCES)

validate: ## judge applied snapshots, collect voltage hints: make validate SOURCES=osm_hamburg ($(ENV))
> $(PY) -m cabler.pipeline validate $(ENV) $(SOURCES)

review: ## substations without a tagged voltage, with evidence: make review SOURCES=osm_hamburg ($(ENV))
> $(PY) -m cabler.pipeline review $(ENV) $(SOURCES) $(if $(SUGGESTION),--suggestion $(SUGGESTION),)

check: ## lint, format check, type check, unit tests
> .venv/bin/poe check

test-int: ## database integration tests against $(ENV)
> CABLER_ENV=$(ENV) .venv/bin/poe test-int

lock: ## refresh the pinned dependency list
> mkdir -p requirements
> $(PY) -m pip freeze --exclude-editable > requirements/dev.lock
