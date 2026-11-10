# ADR-0023: The core holds substations in three nested scenarios; decisions are append-only; review follows impact

Status: accepted · Date: 2026-11-10

## Context
Staging holds every OpenStreetMap substation of both states, and 84% of the 19,659 in NRW have no
voltage tag. Routing needs substations that can take a wind farm's connection, and the honest answer
is a range, not one list. People must be able to decide the cases that matter, and a decision must
never be lost or quietly edited. Nobody can review 16,600 cases, so the effort has to go where a
decision can change a result. And the result has to be reproducible from the raw data.

## Decision
- **The core is derived and rebuildable.** `core.substation` is a function of the newest validated
  snapshots, the wind units, the decisions and a numbered rule set. Each build is recorded in
  `core.build` with its inputs and a hash of its content. The hash leaves out every number the
  database generated, so a rebuild from the raw data must give the same hash; a test does that.
- **Only a validated snapshot is promoted.** A source without a verdict, or whose newest snapshot
  was quarantined, stops the build.
- **Decisions are append-only.** `core.review_decision` refuses UPDATE, DELETE and TRUNCATE: the
  application role lacks the privileges, and a trigger refuses them for every role, the owner
  included. A change of mind is a new row that names the row it replaces; a second trigger allows
  only the newest row to be replaced, so the chain has no forks. A decision beats a tag and a hint.
- **Rules are versioned and never edited.** `core.rule_set` holds the parameters (the voltage limit,
  the reach, which wind units count, the audit sample). A version that exists with other parameters
  is refused.
- **Three nested scenarios.** *Strict*: a tag of at least 110 kV, or a person said high voltage.
  *Likely*: strict, plus untagged substations whose hints point to high voltage. *Inclusive*:
  likely, plus every substation whose voltage is unknown. A tag below the limit is in none. Routing
  runs on all three, so the dependence on uncertain substations is a result, not a hidden choice.
- **Review follows impact.** The impact of a substation is the number of wind units, within the
  reach, for which it lies closer than their nearest strict substation: if it were high voltage, it
  would change their nearest substation. Tier D has no impact and needs no review (half of the
  undecided substations). The rest are tiered by evidence: A conflicting or high-voltage hints, B
  hints agree it is not, C no evidence. The queue lists A and C, most impact first.
- **An audit sample measures the rules.** A stratified sample of the substations with hints is
  drawn by a fixed hash (the same every time) and marked. What the reviewers find there is true
  labelling of untagged substations, which corrects the bias of measuring the rules on tagged ones.
- **One query, no temporary tables.** The application role has no right to create them, and keeps
  it that way; the facts of a build are computed with materialised CTEs.

## Consequences
- On the real data: 20,092 substations; strict 983, likely 1,227, inclusive 17,964. Tiers A 173,
  B 3,666, C 4,433, D 8,709; audit sample 265. A build takes 24 seconds.
- The impact is a straight-line proxy for "would change a route". The routing of a later phase
  replaces it where it matters. Wind units near a border may have a nearer substation outside the
  extract of their state.
- A reviewer is a name in a text field, not an authenticated person; the project has one user.
- The measured precision of the hint rules (leave-tag-out on tagged substations, with the audit
  sample as a check) is the next release.
