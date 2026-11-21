# Wind units in the core

`make windcore` builds `core.wind_unit` from the register units (`staging.wind_unit`), the newest
validated OSM snapshots and the municipal boundaries. The decision and its reasons are in
ADR-0026; this page says how to read the result.

## What a row says
Every unit of the states of the rule set is stored once per build, whatever its status:

| column | meaning |
| --- | --- |
| `status_group` | `existing` (in operation, temporarily shut down), `planned`, `retired` |
| `position_class` | `confirmed`, `plausible`, `doubtful`, `invalid`, `unlocated` |
| `reason` | why: `osm_turbine`, `register_only`, `far_from_municipality`, `isolated_in_farm`, `far_from_municipality_and_isolated_in_farm`, `coordinate_flagged`, `no_coordinate`, `retired` |
| `osm_key`, `osm_distance_m` | the nearest OSM turbine within 100 m, kept even when it does not confirm |
| `registered_distance_m` | distance to the municipality that the register names; 0 inside it, empty when unknown |
| `farm_gap_m` | distance to the nearest other unit of the same MaStR location |
| `place_key` | the municipality that covers the position (as for substations) |
| `in_strict`, `in_likely`, `in_inclusive` | the nested scenarios |

The registered position is never changed. `core.wind_unit_current` shows the newest build.

## Scenarios
- **strict**: existing units with a confirmed or plausible position.
- **likely**: strict, plus planned units with a confirmed or plausible position.
- **inclusive**: likely, plus every existing or planned unit whose position is doubtful.

Routing results can be computed in each scenario and compared. A difference between strict and
inclusive is the effect of the units whose position nobody could confirm.

## Parameters (rules version 1.1.0)
`confirm_m` 25, `match_m` 100, `registered_far_m` 2,000, `farm_gap_m` 5,000, existing statuses
*In Betrieb* and *Vorübergehend stillgelegt*, planned status *In Planung*, the register's own
coordinate flags as invalid. The states come from the OSM sources of the rule set
(`states`), plus the ones in `wind_states` that have no OSM extract. Every parameter is stored
with its version in `core.wind_rule_set`, and each build names its version, so a change of a limit
is a new version and old builds stay readable. Distances are rounded to 0.1 m before the limits are
tested.

## Measured on the real data (NRW and Hamburg)
| scenario | units |
| --- | --- |
| strict | 3,955 |
| likely | 6,091 |
| inclusive | 6,203 |

Of the 6,329 existing and planned units: 3,813 positions are confirmed, 2,278 plausible, 112
doubtful, 14 invalid and 112 unlocated. 505 retired units are stored but in no scenario. 4,240
units (4,072 of them existing or planned) have an OSM turbine within 100 m. 402 of the 4,425 OSM
turbines have no active unit within 100 m; a turbine that two extracts hold is counted once.

Of the existing and planned units whose registered municipality is more than 2 km from the position,
567 are confirmed by an OSM turbine at the position, 112 are doubtful and 14 invalid. The registered
key is therefore the weaker evidence of the two, and a turbine outranks it.

## Limits
- *Confirmed* means that an OSM turbine stands there, not that it is this unit: two units of one
  wind farm can be exchanged without anyone noticing, which does not matter for routing.
- *Plausible* is not verified. For planned units there is usually no turbine yet.
- Confirmation exists only where an OSM extract is a source: NRW and Hamburg.
- The register's units have no validation verdict yet, unlike the OSM snapshots.
- The nearest-turbine lookup compares every unit with every turbine; it takes 7 s here and needs an
  index or a grid for a country-wide build.
