# ADR-0002: PostgreSQL 17, PostGIS 3.5 and pgRouting in Docker

Status: accepted · Date: 2026-11-07

## Context
The project needs identical, disposable, isolated databases per environment, and pgRouting for the
parcel graph in a later phase.

## Decision
- Build the database image from the official `pgrouting/pgrouting:17-3.5-3.8.0` image, pinned to an
  exact tag (PostgreSQL 17, PostGIS 3.5, pgRouting 3.8.0).
- One container, volume and port per environment (dev 5444, staging 5442, production 5443), bound
  to `127.0.0.1` only.
- Passwords are generated per environment, written to a gitignored file with mode 0600, and passed
  to the container as environment variables. They never appear in git.

## Consequences
- Staging and production use exactly the image that was tested in dev.
- Rasters are not stored in the database (see ADR-0004).
