# ADR-0003: Least-privilege roles and schemas that follow the data flow

Status: accepted · Date: 2026-11-07

## Context
The application, analysts and migrations should not share one all-powerful database account.

## Decision
- Three login roles, none a superuser: `cabler_owner` (owns objects, runs migrations),
  `cabler_app` (read/write data, no structural changes), `cabler_ro` (read only).
- Schemas follow the data flow: `raw` -> `staging` -> `core` -> `results`, plus `meta`.
- Default privileges on every schema give new tables the right access automatically.
- Gate tests connect as each role and prove the model, including the failures.

## Consequences
- A compromised application account cannot drop or alter tables.
- Every migration runs as `cabler_owner`.
