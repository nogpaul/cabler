# ADR-0007: Data acquisition as an update-aware pipeline

Status: accepted · Date: 2026-11-08

## Context
The project depends on external open data (turbines, substations, terrain, land cover, parcels,
protected areas) from several publishers and two states, designed to scale to every German state.
Sources change over time, and a one-off download would make results unreproducible and stale.

## Decision
Every source passes through the same stages, each a typed function returning structured results:

1. **registry**: licence, attribution, allowed host, format, update method, area of interest
2. **check**: ask the publisher whether something changed, without downloading (dry run)
3. **fetch**: download only if changed; allow-listed hosts, size limit, resumable, never executed
4. **verify and store**: SHA-256, then an immutable versioned snapshot outside the repository
5. **load**: snapshot into the `raw` schema, tagged with its version, exactly as delivered
6. **diff**: compare with the previous snapshot by stable feature ID: added, changed, removed
7. **apply**: clean current state in `staging`/`core`, with full history (valid from, valid to)
8. **validate**: per-source rules; a failing snapshot is quarantined and never reaches `core`
9. **report**: every run recorded in `meta.pipeline_run` with a structured result

State-specific code lives in adapters and per-state configuration only.

## Consequences
- Re-running is safe and cheap: unchanged sources cost one lightweight request.
- Every result is traceable to a source version and a run.
- Licences and credit lines are data, so maps and atlases can print them automatically.
- Because stages are typed and return structured data, they can be driven by automation later.
