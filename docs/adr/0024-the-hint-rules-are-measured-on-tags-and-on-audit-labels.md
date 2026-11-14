# ADR-0024: The hint rules are measured on tagged substations and on audit labels

Status: accepted · Date: 2026-11-10

## Context
The scenarios of ADR-0023 depend on hints: a nearby line with a voltage and the substation type
turn an untagged substation into `hv_candidate`, `conflict`, `likely_low_voltage` or
`no_evidence`. Without a measurement, "likely" is a guess. The difficulty is that nobody knows the
true voltage of an untagged substation, which is exactly the case the rules are for.

## Decision
- **Leave-tag-out.** Every substation that carries a voltage tag is judged by the same rules as if
  it had none, and the answer is compared with the tag (high means at least 110 kV). This gives
  thousands of cases, but they describe tagged substations, which are mapped more carefully.
- **Audit labels.** The audit sample is a random draw from the untagged substations that have a
  hint and a wind unit in reach (ADR-0023). What people decide about it is the true label. The draw
  is random within each hint, so the interval is valid for that group, but there are few cases.
  Both are reported side by side, so a gap between them shows the bias.
- **Wilson score intervals, 95%.** They stay honest for small counts and for shares near 0 or 1,
  where the normal interval fails (it gives zero width for 0 of 10). The report always shows the
  count next to the share and shows no share at all when nothing was counted.
- **The rules exist once as a rule and once as a measurement.** The evaluation evaluates every
  substation with the same SQL logic as `staging.substation_review`; a test compares the two on
  the untagged substations of a fixture, so a change of the rules that is not copied fails.
- **Nothing is stored.** The evaluation is a function of the snapshot, the core build and the
  decisions, so `make evaluate` reproduces it. The dated results are in
  `docs/rules-evaluation.md`.

## Consequences
- On the real data (snapshots osm_nrw 397, osm_hamburg 25): of 932 tagged NRW substations judged
  `hv_candidate`, 97.0% are high voltage (95.7% to 97.9%); `likely_low_voltage` is high voltage in
  0.1% of 1,358; `conflict` in 3.4% of 29; `no_evidence` in 3.0% of 727.
- The tagged group is not the untagged group. The numbers say how well the rules work where the
  answer is known; the audit labels (0 of 481 labelled so far) say how well they work where it is
  not. Until labels exist, the scenarios keep their rules and the measurement stays a warning, not
  a correction.
- A later release can replace the three fixed scenarios by weights taken from these shares; that
  needs the audit labels first.
