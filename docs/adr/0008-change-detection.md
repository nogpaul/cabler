# ADR-0008: Change detection between snapshots

Status: accepted · Date: 2026-11-08

## Context
Sources are re-fetched over time (ADR-0007). Downstream stages need to know what changed, not just
that something did: staging keeps history, validation needs a sanity check on the size of a change,
and a report has to say what is new. OpenStreetMap objects have a stable id and a version number,
but a way keeps its version when one of its nodes moves, so the version alone misses real changes.

## Decision
The **diff** stage compares each loaded snapshot with its predecessor and stores one row per
object in `raw.osm_change`:

- objects are matched on `(layer, osm_key)`; the key is the stable feature id (`n`/`w`/`r` + id)
- **added**: only in the new snapshot; **removed**: only in the old one
- **changed**: any stored column differs, or the geometry differs; `changed_fields` lists exactly
  which columns (and `geometry`), so a re-tag, a reshape and a bare version bump are told apart
- a source's first snapshot has no predecessor: every object is stored as added, which makes the
  next stage uniform
- each snapshot is diffed in its own transaction, oldest first, then marked `diffed`; a snapshot
  can only be diffed once, so re-running is safe
- the run result carries counts, totals before and after, and a histogram of changed fields, as
  plain JSON in `meta.pipeline_run`

Status order of a snapshot: `discovered`, `fetched`, `loaded`, `diffed`, `applied`
(`failed` and `quarantined` as side exits).

## Consequences
- The comparison is one set-based SQL statement; 3,400 objects take well under a second.
- A change of the theme selection in a source file shows up as a mass of additions or removals.
  Validation is expected to catch implausible change sizes before anything is applied.
- Snapshots of one source stay in `raw.osm_object` until a retention rule is decided; the change
  sets are small and kept for good.
