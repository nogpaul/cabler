# ADR-0027: Every layer is judged by one exit check

Status: accepted · Date: 2026-11-15

## Context
The exit criterion of the data phase is "all layers load, valid, CRS-correct, lineage recorded".
Until now only the OSM power snapshots had a verdict (ADR-0011); the register of wind units,
parcels, land cover, protected areas, boundaries and terrain were checked only by the findings of
their own load and apply stages. Whether a layer is fit to build the next phase on was a matter of
remembering what had been looked at. Two designs were considered:

- **A validator per source type, like the one for OSM.** Rejected for the layers. It would repeat
  the same questions seven times, and a new layer would need a new validator before anything could
  be said about it.
- **One table-driven check over the current rows of every layer, with the validate stage kept for
  the sources whose snapshots can be judged as a whole.** Chosen.

## Decision
- **One check, one scan per layer.** `cabler.layercheck` reads the current rows of a layer once and
  counts, per source: rows without a geometry, geometries in another reference system, geometries
  that are not valid, geometries outside the envelope of Germany, and rows whose snapshot is not in
  the catalogue, is of another source, has no checksum or was never applied. Quality flags that the
  apply stage set (`geometry_repaired`, `area_mismatch`, `coordinate_placeholder`, ...) are counted
  as well. A layer is a row of a table in the module (table, geometry column, which rows are
  current, which column names the snapshot), so a new layer is one entry.
- **Errors fail, warnings inform.** Missing or invalid geometry, a wrong reference system, broken
  lineage, a missing licence, a land cover code that is not in the CORINE legend and a source
  whose newest applied snapshot has no passed verdict are errors; features outside Germany are a
  warning up to 1% of a source and an error above it. The flags describe the data and never block.
  Units of the register without a coordinate are a warning (the register has none for some), every
  other layer's missing geometry an error.
- **Sources with a validate stage need a verdict.** OSM power sources and the register of the wind
  units: the newest applied snapshot must have been judged, and the verdict must be *passed*.
- **The register of wind units gets a validator** (`validate` for sources with the wind theme):
  an empty state and the removal of more than 20% of the units are errors, as for OSM; the shares
  of units without a coordinate (warning above 2%, error above 30%), with a placeholder, outside
  the state or outside Germany (warning above 0.1%, error above 5%) and without or with an
  implausible capacity (warning above 0.5%, error above 5%) are judged like the flags of OSM.
  The core wind units now require this verdict, as the substations require the OSM verdict.
- **A report is evidence.** Each run is stored as one `meta.check_run` with its
  `meta.check_result` rows (all of them, also the ones without a finding), in tables that refuse
  UPDATE, DELETE and TRUNCATE; the view `meta.check_result_current` shows the newest one.
  `make exit-check` prints only what is not ok and exits with 1 on an error.
- **The wind build records its level of isolation** (migration 0021) and the command requires one
  snapshot. A rules version names one set of parameters: the same version with other parameters
  is refused. These close the open findings of the review of v0.21.1.

## Consequences
- The check says what it cannot see: a layer without current rows is an error ("no current rows"),
  so an environment that holds only the schema fails it. The exit check is run on the environment
  that holds the data; staging and production hold only the schema until the first release that
  needs their data.
- The geometry validity scan reads every geometry once. For NRW and Hamburg (10.6 million rows,
  among them 9.4 million parcels) the whole check takes about 100 seconds. `--fast` skips the
  validity scan for a quick look, and the stored options say that it was skipped.
- Snapshot files are checked for their presence only (a warning if gone), not hashed again; the
  checksum is verified when a snapshot is fetched and by `tiles --verify`.
- The thresholds of the register validator are first values chosen from the real register (3.0% of
  the units have no coordinate, two a placeholder); they are constants of `cabler.validate` and
  change with a release.
