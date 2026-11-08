# ADR-0009: Staging holds the cleaned current state, with history

Status: accepted · Date: 2026-11-08

## Context
The raw schema keeps data exactly as delivered, in WGS 84, with free-text tags. Routing needs
classified, projected, trustworthy objects, and results must be reproducible for any past
snapshot. Real OpenStreetMap power data is incomplete: of 433 Hamburg substations, 368 carry no
voltage tag; voltages come as lists ("380000;110000") or words ("medium"); generator outputs come
in W, kW and MW, or as "yes".

## Decision
The **apply** stage maintains `staging.osm_power_feature`, one row per object version:

- a row is *current* while `valid_to_version_id` is NULL; a changed or removed object has its row
  closed by the snapshot that changed it, a changed or added object gets a new current row;
  nothing is deleted, and `staging.osm_power_as_of(source, snapshot)` returns the state at any
  applied snapshot
- only the objects named in the snapshot's change set are touched (ADR-0008)
- geometry is stored in EPSG:25832, repaired if invalid (flagged), plus a representative point
- objects are classified (`substation`, `line`, `cable`, `minor_line`, `wind_turbine`, `generator`,
  `support`, `other`) by plain SQL functions, so the same rules run on every state's data
- the voltage is the highest plain number in the tag; grid assets get a voltage class
  (`extra_high` from 220 kV, `high` from 110 kV, `medium_low`, `unknown`)
- **objects with missing or unreadable values are kept and flagged, not dropped**: `qa_flags` lists
  `voltage_missing`, `voltage_unparsable`, `output_missing`, `output_unparsable`,
  `geometry_repaired`; the OSM `substation=*` type is kept as a hint (for example `transmission`
  or `minor_distribution`) so a later rule can decide, with the reason recorded
- each snapshot is applied in its own transaction, oldest first, then marked `applied`

Staging is not yet trusted: validation (next stage) decides what is promoted to `core`.

## Consequences
- Dropping unknown-voltage substations would silently lose real grid connection points; flagging
  keeps them visible and counted in every run result.
- The parsing and classification rules are tested in the database against fixed examples.
- Only OSM sources with the power theme have an applier; other sources get their own.
- Rows accumulate with every change; the history is small compared with the raw snapshots.
