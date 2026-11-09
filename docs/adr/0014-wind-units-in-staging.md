# ADR-0014: Wind units in staging

Status: accepted · Date: 2026-11-09

## Context
The register lists every wind unit in Germany with its status, size and a position that the
operator typed in. Planned units matter as much as operating ones, because a cable route is built
for a wind farm that does not yet turn. The register has codes for most descriptions (status,
manufacturer, onshore or offshore), positions that are missing, a known placeholder (6.0 East,
50.0 North), obvious typos (latitude 60 for a unit in Paderborn) and coordinates in the wrong
state.

## Decision
- **`staging.wind_unit`** keeps one row per version of a unit, with the same history convention
  as `staging.osm_power_feature` (a row is current until it is closed by the snapshot that changed
  or removed the unit). All states are stored; `state_key`, the first two digits of the municipality
  key, selects one. Adding a state does not change code.
- **Catalogue codes are decoded** from the catalogue delivered in the same snapshot. The codes stay
  in raw.
- **Nothing is corrected.** A suspicious position is stored as delivered and carries one finding in
  `qa_flags`, the most specific that applies: `coordinate_missing`, `coordinate_placeholder`,
  `coordinate_outside_germany` (a box wide enough for offshore units) or
  `coordinate_outside_state` (a coarse box per state, `staging.state_box`, two states filled so
  far). Capacity and date problems are flagged the same way. Whether to use, repair or drop a
  flagged unit is a decision of the next stage, visible and reversible.
- **No unit is dropped for being small or planned.** Selecting by status or size is a query on
  staging, not a loss of data.

## Consequences
- The coordinate check is a box, not a border: a point inside its state's box is not proven right,
  a point outside is wrong. A precise check against the state polygon follows once boundaries
  are loaded.
- A decoded description is decoded when the unit's row is opened. If the publisher rewords a
  catalogue entry, units that did not change keep the old wording until they change.
- Findings are counted on every apply, so a worsening publisher export is visible in the run
  history.
