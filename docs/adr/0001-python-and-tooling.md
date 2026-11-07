# ADR-0001: Python 3.12, src layout, strict quality tooling

Status: accepted · Date: 2026-11-07

## Context
Geospatial packages (rasterio, numba, scikit-image) lag new Python releases. Development runs on
Ubuntu 24.04, which ships Python 3.12.

## Decision
- Target Python `>=3.12,<3.14`, developed in a project-local `.venv`.
- `src/` layout and a single package `cabler`, built with hatchling.
- Quality gate: ruff (lint and format), mypy in strict mode, pytest with an 80% coverage floor.
- Tasks run through `make` (shortcuts) and `poethepoet` (the commands CI runs).
- Dependencies are pinned in `requirements/dev.lock`.

## Consequences
- Python 3.14 is unsupported until the geospatial stack ships wheels for it.
- Every change is held to the same standard locally, in pre-commit and in CI.
