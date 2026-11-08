# cabler

Optimal cable routing from wind farms to substations: a reproducible, tested pipeline built on
PostgreSQL, PostGIS and pgRouting.

## Quick start (Ubuntu / WSL2)

    make setup      # project-local Python 3.12 environment + dev tools
    make env        # env/dev.env with random passwords (gitignored)
    make db-up      # PostgreSQL 17 + PostGIS 3.5 + pgRouting 3.8 in Docker
    make migrate    # apply database migrations
    make register   # write the data sources and their licences to the database
    make check      # lint, format, types, unit tests
    make test-int   # database gate tests

Use `ENV=staging` or `ENV=production` to target the other environments, for example
`make db-up ENV=staging`. Run `make help` for every shortcut.

## Environments

| Name | Port | Purpose |
|---|---|---|
| dev | 5444 | development, disposable |
| staging | 5442 | release candidate on real data |
| production | 5443 | tagged releases only |

All ports bind to `127.0.0.1` only.

## Database

| Role | Purpose |
|---|---|
| `cabler_owner` | owns objects, runs migrations |
| `cabler_app` | read/write data, no structural changes |
| `cabler_ro` | read only |

Schemas follow the data flow: `raw` -> `staging` -> `core` -> `results`, plus `meta` for bookkeeping.
All geometry is stored in ETRS89 / UTM 32N (EPSG:25832).

## Quality gate

`make check` runs ruff (lint and format), mypy in strict mode and pytest with an 80% coverage floor.
`make test-int` proves the database setup, including the privilege model, against a running
environment. The same commands run in CI on every push.

## Roadmap

1. Foundations: tooling, environments, database, quality gate (this release)
2. Data acquisition and ingestion
3. Cost surface modelling
4. Baseline least-cost routing and benchmark against ArcGIS Spatial Analyst
5. Cable cost model and network design
6. Parcel-aware and uncertainty-aware routing
7. Digitised-route costing
8. Atlas, web app and 3D visualisation
9. Hardening, scale-out to further states, release
