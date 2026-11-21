# ADR-0026: Wind units in the core, with the quality of their position

Status: accepted · Date: 2026-11-15

## Context
Routing starts at wind units. The register (MaStR) gives the position of every unit, but nobody
checked it: 112 of the 6,329 existing and planned units of NRW and Hamburg have none, 14 are
flagged as impossible, and the municipality that the register names for a unit disagrees with the municipality that covers its coordinate for 16.5% of
the NRW units (ADR-0025). Two choices were considered for a coordinate that looks wrong:

- **Drop it, or move the unit to the OSM turbine nearby.** Rejected. Dropping loses real turbines
  whose coordinate is merely sloppy, and moving mixes two sources so that nobody can tell where a
  position came from.
- **Keep every position as registered, and store how much it can be trusted and why.** Chosen.
  The decision to use a doubtful position becomes a scenario that can be measured, not a silent
  filter.

## Decision
- **Position classes.** `confirmed`: an OSM turbine within `confirm_m` (25 m) of the position, an
  independent source agrees. `plausible`: nothing speaks against it. `doubtful`: no confirmation,
  and the position lies more than `registered_far_m` (2 km) from the municipality that the register
  names, or more than `farm_gap_m` (5 km) from every other unit of the same MaStR location (wind
  farm). `invalid`: the register's own flag (placeholder, outside the state, outside Germany).
  `unlocated`: no coordinate.
- **Evidence is stored, nothing is moved.** The nearest OSM turbine within `match_m` (100 m) is kept
  with its key and distance even when it does not confirm; the registered distance and the gap to the
  wind farm are kept too. The registered position stays the position.
- **Scenarios**, nested like those of the substations: `strict` = existing units (in operation, or
  temporarily shut down) whose position is confirmed or plausible; `likely` adds the planned units
  with such a position; `inclusive` adds every existing or planned unit with a doubtful position.
  Retired units, invalid and unlocated positions are in no scenario but stay in the table with the
  reason, so the counts add up.
- **Rules are recorded with each build.** `core.wind_build` stores the rules version, the exact
  parameters, the versions of every input (OSM snapshots, boundaries, units) and a hash of the
  content. The table refuses UPDATE, DELETE and TRUNCATE like the other core tables, and a rebuild
  from the same inputs gives the same hash.
- **A key the boundaries lack proves nothing.** A unit whose registered key is not in the boundaries
  has no registered distance and is judged by the other evidence only.
- **One snapshot.** The build reads inside a REPEATABLE READ transaction (unless the caller has
  opened a transaction, which then decides), so the units, the turbines and the recorded input
  versions describe the same state of the database.
- **Distances are rounded to 0.1 m** before the limits are tested, so a limit is exact to 5 cm.
- **States.** A state is built from the OSM sources of the rule set, or named in `wind_states`
  when it has no OSM extract; the rule set names at least one state.
- **`make windcore`** builds it. OSM turbines without a unit within reach are counted, not guessed.

## Consequences
- On the real data (NRW and Hamburg) 6,329 units are existing or planned, and 505 retired units are
  stored but in no scenario. Of the 6,329, 3,813 positions are confirmed by an OSM turbine, 2,278
  are plausible, 112 doubtful, 14 invalid and 112 unlocated. The scenarios hold 3,955 units
  (strict), 6,091 (likely) and 6,203 (inclusive).
- Of the units that the register places more than 2 km from their named municipality, 567 stand at
  an OSM turbine. For them the coordinate is right and the registered **key** is the weak side.
  Taken together with ADR-0025 this corrects the reading there: the key is not a reliable test of a
  coordinate, which is why a turbine outranks it, and why the municipality test alone would have
  thrown away 567 good positions.
- 2,278 positions (mostly planned units, which have no turbine to find yet) are plausible without
  any independent check. The share of confirmed positions is the measure of the evidence, and the
  scenarios let a later analysis ask what changes if they are wrong.
- OSM covers only NRW and Hamburg in this data, so confirmation works for those two states. A state
  without an OSM extract is built all the same (`wind_states`); nothing can confirm its positions,
  so every position there is at best plausible.
- The unmatched OSM turbines (402) are candidates for units that the register lacks or places
  wrongly; they are counted, once even when two extracts hold the same OSM object, and reviewing
  them is a later step.
- The register's units are not validated the way the OSM snapshots are: no verdict exists for
  them yet. A gate for them belongs with the validation reports.
- The lookup of the nearest turbine compares every unit with every turbine (7 s for NRW and
  Hamburg). A country-wide build needs an index or a grid; that is a scale item for the hardening
  phase.

## Amendment (v0.23.0): the rules get a table of their own
A rules version was defined by the parameters of the first build that used it, and every build
compared its parameters with that build. Two builds that started at the same time with a new version
could both pass the comparison and store two different meanings of one version. Now
`core.wind_rule_set` holds one row per version, with the version as its primary key, and every build
names its version by a foreign key; the parameters moved there from `core.wind_build`. A build
stores its version if it is new and refuses a version that exists with other parameters; two builds
at the same time cannot both store one, the second fails. The table refuses changes like the other
core tables. Migration 0022 moved the two versions of the real builds (1.0.0 and 1.1.0); it refuses
to run if a build had other parameters than the first build of its version.
