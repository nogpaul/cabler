# Where are the substations and the wind units?

A substation has a position but, mostly, no name. Its *place* is the municipality that covers the
position, taken from the official boundaries (BKG VG250, ADR-0025), together with the district. A
wind unit has a position and a municipality key from the register (MaStR). This page says what
the places deliver, what they cannot tell and how the register's key compares with the position.
Run it yourself:

```bash
make places              # the environment is set by ENV, as for every other command
```

The boundaries are loaded like every other source: `make fetch SOURCES=bkg_vg250 YES=1`, then
`make load`, `make diff` and `make apply` with the same source. A core build (`make core`) then
gives every substation its place.

## Results of 2026-11-14

Boundaries: VG250, edition 1 January 2026, 11,094 municipality rows and 434 districts. The core
build is the one of the same day.

Substations:

| Source | Substations | With a place | Without | In another state than the extract |
|---|---:|---:|---:|---:|
| osm_hamburg | 433 | 433 | 0 | 19 |
| osm_nrw | 19,659 | 19,644 | 15 | 19 |

The 15 without a place lie at the Belgian and Dutch border near Aachen and are outside Germany. The
extracts of Geofabrik are cut along a polygon that reaches into the neighbouring states, which is
where the substations of another state come from.

Wind units, the municipality of the register against the one that covers the coordinate (units in
the statuses *in operation* and *planned*, without a finding on the coordinate):

| State | Units | Same | Share | Up to 100 m | to 500 m | to 2 km | to 10 km | Beyond |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 02 Hamburg | 64 | 64 | 100.0% | 0 | 0 | 0 | 0 | 0 |
| 05 NRW | 6,130 | 5,117 | 83.5% | 46 | 93 | 196 | 527 | 151 |

The distance columns count the units whose coordinate lies outside the municipality that the
register names, by how far the coordinate is from it. Every key of the register exists in the
boundaries, so out-of-date keys after a merger of municipalities do not explain the differences.

## What this says, and what it does not

- Almost every substation has a place, and the place is a fact about the position, not a guess.
- A unit beside a border is not an error. Of the 1,013 NRW units that differ, 139 (14%) are within
  500 m of the named municipality, where the generalisation of the boundaries (1:250 000) alone can
  explain the difference. The other 874 are farther, 678 of them by more than 2 km.
- In Hamburg the two sources agree for every unit; in NRW one unit in six does not. The coordinate
  that an operator types in is therefore weaker evidence of where a turbine stands than the key of
  its municipality. The wind-unit core has to treat a coordinate that is kilometres from the
  registered municipality as doubtful, not as a position to route from.
- The boundaries tell the place of a point, not the owner of the land and not the route: parcels
  (`staging.parcel`) are the cadastral record for that.
