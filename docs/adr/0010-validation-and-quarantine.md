# ADR-0010: Validation, quarantine and evidence for untagged voltages

Status: accepted · Date: 2026-11-08

## Context
Staging holds whatever the latest snapshot said (ADR-0009). A publisher can deliver a broken file:
half the country missing, an empty extract, objects at the wrong place. Separately, most
substations in OpenStreetMap carry no voltage tag (368 of 433 in Hamburg), and no official, open,
nationwide substation dataset with voltages was found. Stromnetz Hamburg reports 55 substations at
110/10 kV, so the high-voltage ones matter, but they cannot be told apart from the many small
distribution stations by their tags alone.

## Decision
The **validate** stage gives every applied snapshot a verdict and gathers evidence:

- **Verdict**: `staging.snapshot_validation` holds `passed` or `quarantined` per snapshot. Any error
  rule quarantines the snapshot. A quarantined snapshot stays in staging, so the history and the
  next diff stay consistent, but only `passed` snapshots may be promoted. The snapshot's status in
  `meta.source_version` is unchanged.
- **Snapshot rules**: empty state (error); more than 20 % of the objects vanishing at once (error);
  objects outside a generous Germany envelope in EPSG:25832 (warning, error above 1 %); repaired
  geometries (warning above 1 %, error above 10 %). Thresholds are constants in `cabler.validate`.
- **Voltage hints** for substations without a usable voltage, never overwriting a tagged value:
  - `voltage_hint_line`: the voltage of the nearest line or cable of known voltage within 50 m
  - `voltage_hint_type`: `substation=minor_distribution` suggests low voltage,
    `substation=transmission` suggests high voltage
  - `voltage_hint_conflict`: the two disagree
- Findings go to `staging.validation_finding` with structured detail; `staging.substation_review`
  turns them into a suggestion per substation (`hv_candidate`, `likely_low_voltage`, `conflict`,
  `no_evidence`). **A suggestion is not a decision**: a person confirms or rejects, with a reason,
  at promotion.

## Consequences
- On Hamburg, 27 of 368 untagged substations have a line within 50 m; 9 of these conflict with a
  `minor_distribution` tag, and the hits include named transformer stations of the operator as
  well as industrial plants. Proximity is evidence, not proof, and the review queue stays.
- Where the publisher's delivery is implausible, nothing downstream moves until a person looks.
- Official operator data and the MaStR connection voltage can be added later as further hint
  sources without changing the verdict logic.
