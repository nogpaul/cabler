# How well do the voltage hints work?

Most OpenStreetMap substations carry no voltage tag, so the core (ADR-0023) uses hints: a nearby
line with a voltage, and the substation type. This page says how reliable those hints are, how that
is measured and what the measurement cannot tell. The decision is ADR-0024. Run it yourself:

```bash
make evaluate            # the environment is set by ENV, as for every other command
```

## The rules

| Hint | When | Means |
|---|---|---|
| `hv_candidate` | a line of at least 110 kV within 50 m, or the type is `transmission` | probably high voltage |
| `conflict` | the line says 110 kV or more and the type says minor distribution, or the other way round | the evidence disagrees |
| `likely_low_voltage` | the type is `minor_distribution`, or a line below 110 kV is near | probably not high voltage |
| `no_evidence` | none of the above | nothing is known |

## Two measurements

1. **Leave-tag-out.** Each substation that has a voltage tag is judged as if it had none, and the
   hint is compared with the tag (high means at least 110 kV). This gives thousands of cases, but
   it describes tagged substations, which are mapped more carefully than the others.
2. **Audit labels.** A fixed, random sample of the untagged substations that have a hint and a wind
   unit in reach is marked in every core build. What people decide about them (`make queue`, then
   `make decide`) is the true label. These are few, but they are the group the rules are for.

Every share has a 95% Wilson score interval. Read the interval before the share: 3 of 7 is 43%,
and the interval runs from 16% to 75%.

## Results of 2026-11-10

Snapshots osm_nrw 397 and osm_hamburg 25. Leave-tag-out, high means a tag of at least 110 kV:

| Source | Hint | Tagged substations | Of them high voltage | Share | 95% interval |
|---|---|---:|---:|---:|---|
| osm_nrw | `hv_candidate` | 932 | 904 | 97.0% | 95.7% to 97.9% |
| osm_nrw | `conflict` | 29 | 1 | 3.4% | 0.6% to 17.2% |
| osm_nrw | `likely_low_voltage` | 1,358 | 2 | 0.1% | 0.0% to 0.5% |
| osm_nrw | `no_evidence` | 727 | 22 | 3.0% | 2.0% to 4.5% |
| osm_hamburg | `hv_candidate` | 51 | 51 | 100.0% | 93.0% to 100.0% |
| osm_hamburg | `likely_low_voltage` | 7 | 0 | 0.0% | 0.0% to 35.4% |
| osm_hamburg | `no_evidence` | 7 | 3 | 42.9% | 15.8% to 75.0% |

Audit labels: 481 substations are drawn (Hamburg 57, NRW 424), none is labelled yet.

## What this says, and what it does not

- Where a tag exists, a high-voltage hint is right nearly always, and a low-voltage hint is wrong
  in about one case in a thousand. Of the 929 tagged NRW substations that are high voltage, the
  rules call 904 `hv_candidate` (97.3%); 22 of the rest have no evidence at all.
- A `conflict` is nearly always a low-voltage substation. It is not a sign of an important one.
- Hamburg has few tagged substations, so its intervals are wide. The 43% for `no_evidence` is
  3 of 7, and almost nothing can be concluded from it.
- **The tagged substations are not the untagged ones.** A mapper who types a voltage usually
  maps the substation with more care, and may have mapped it because it is large. The leave-tag-out
  numbers are therefore the best case. A line that a mapper tagged by copying the voltage of its
  substation also makes the hint look better than it is. Only the audit labels measure the
  untagged substations, and until there are enough of them the scenarios are not corrected by
  these numbers.
- The straight-line distance of 50 m and the substation types are all the evidence the rules use.
  Operator names, the number of transformers and the connection of a line to the substation are
  not used.
