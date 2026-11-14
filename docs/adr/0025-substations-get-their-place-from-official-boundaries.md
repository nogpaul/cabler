# ADR-0025: Substations get their place from official boundaries

Status: accepted · Date: 2026-11-14

## Context
Most OpenStreetMap substations have no name (86% of NRW, 88% of Hamburg), so a person who has to
decide about one (ADR-0023) cannot tell where it is. Two ways to give it a place were considered:

- **Infer a name or a locality from a postcode.** Rejected. A postcode covers many objects, so
  copying the name of one substation to an unnamed neighbour would state something nobody checked.
  Only about 5% of the unnamed NRW substations carry an `addr:postcode` tag in the first place.
- **Look the place up from the position.** The municipality that covers the point is a fact about
  the point, it exists for every substation, and it needs no tag.

## Decision
- **Boundaries.** BKG VG250 (1 January edition, GeoPackage, EPSG:25832, dl-de/by-2-0, credit
  "© BKG (2026)"), as the source `bkg_vg250`. It goes through the same stages as every source
  (fetch, load, diff, apply) into `staging.admin_area`, with history. Municipalities and districts
  are loaded; the state is not needed yet.
- **Identity.** The publisher's `OBJID` is the feature key. The municipality key (`AGS`) is not
  unique: a coastal municipality has a land row and a water row with the same key.
- **The lookup** is a point-in-polygon test (`ST_Covers`). A point on an edge is covered by both
  neighbours, and a point in the water part of a municipality by a water row, so land comes first
  and the lowest key breaks a tie. The result never depends on the order of the rows.
- **Where it lives.** `core.substation` gets `municipality_key`, `municipality` and `district`. The
  place is part of the content hash, and the rule set names the boundary source
  (`admin_source`), so the default rule set is a new version, 1.1.0. A build records the version of
  the boundaries it used (0 when there were none, and then no substation has a place).
- **Nothing is invented.** A point outside every boundary has no place; a source without applied
  boundaries produces no places and no error.
- **`make places`** measures the result: how many substations have a place, how many lie in another
  state than their extract, and how well the municipality that MaStR names for a wind unit agrees
  with the municipality that covers its coordinate, with the distance for the ones that do not.

## Consequences
- On the real data 20,077 of 20,092 substations have a place. The 15 without lie at the Belgian and
  Dutch border and are outside Germany.
- The Geofabrik extracts reach into the neighbouring states: 19 substations of the NRW extract lie
  in Lower Saxony, Hesse or Rhineland-Palatinate, and 19 of the Hamburg extract in Schleswig-Holstein
  or Lower Saxony. They are kept; the place shows where they are.
- VG250 is generalised at 1:250 000, so a point within some hundred metres of a border can get the
  neighbour. The place is for orientation and review, not for legal use.
- The MaStR key and the coordinate of a wind unit disagree for 16.5% of the NRW units that the core
  uses; for 151 of them the coordinate is more than 10 km from the named municipality (see
  `docs/places.md`). That is a finding about the register's coordinates, to be used by the wind-unit
  core; this ADR does not change any unit.
- The `addr:*` tags of OSM are not read yet. They are not in staging, and adding them means
  re-applying the OSM sources; a later release can store them as given, beside the derived place.
