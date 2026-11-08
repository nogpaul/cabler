# ADR-0006: Versioned plain-SQL migrations with a small in-package runner

Status: accepted · Date: 2026-11-08

## Context
Phase 1 introduces the first real tables. Schema changes must be reproducible, reviewable and
applied identically in every environment. The schema is PostGIS-centric, so hand-written SQL is
clearer than generated code, and the project has no ORM.

## Decision
- Migrations are `db/migrations/NNNN_description.sql`, numbered 1..N without gaps.
- `cabler.migrate` applies pending files in order, each in its own transaction, as `cabler_owner`.
- Applied migrations are recorded in `meta.schema_migration` with a SHA-256 checksum. Editing an
  applied file, or a database ahead of the files, stops the run with an error.
- Migrations are forward-only. A mistake is corrected by a new migration, never by editing history.
- The runner is about 150 lines, fully unit- and integration-tested, and has no extra dependency.

## Consequences
- Every environment reaches the same schema by running `python -m cabler.migrate up <env>`.
- No automatic rollback; destructive changes need a deliberate follow-up migration.
- A failed migration leaves no partial change, because each runs in a transaction.
