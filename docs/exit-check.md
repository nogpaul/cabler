# The exit check of the data phase

`make exit-check` judges every layer of the staging schema with the same questions and stores the
answer. It is the one command that says whether the data phase is done. The decision and its
reasons are in ADR-0027; this page says how to read the result.

## What is checked
For every source in every layer, over the **current** rows:

| check | finds | severity |
| --- | --- | --- |
| `geometry_missing` | rows without a geometry | error (a warning for the register of wind units, which has none for some units) |
| `crs_not_25832` | a geometry in another reference system | error |
| `geometry_invalid` | a geometry that is not valid after the repair of the apply stage | error |
| `outside_germany` | a geometry outside the envelope of Germany | warning, error above 1% of the source |
| `lineage_snapshot_missing` | a row whose snapshot is not in the catalogue or belongs to another source | error |
| `lineage_snapshot_unsealed` | a row whose snapshot has no checksum or was never applied | error |
| `licence_missing` | a source without licence, licence URL or credit line | error |
| `verdict` | OSM power sources and the wind unit register: the newest applied snapshot has no verdict, or it is not *passed* | error |
| `snapshot_file` | the file of the newest snapshot is gone | warning |
| `flag:<name>` | rows carrying a quality flag of the apply stage | warning (information for `geometry_repaired`) |
| layer-specific | a land cover code that is not in the CORINE legend; an area without its key; a terrain tile in another reference system | error |

A layer without current rows is an error: the check cannot judge what is not there.

## Reading the print
One block per layer, one line per source with its worst severity, and under it every check that is
not ok with the number of bad rows, the total and the share. The last line is the verdict:
`exit check PASSED` or `FAILED`, with the number of errors and warnings. The exit code is 1 on an
error. `--fast` skips the geometry validity scan (a partial check, stored as such), `--no-files`
does not look for the snapshot files.

## The stored report
Every run is written to `meta.check_run` and `meta.check_result` (all results, also the ones
without a finding). The tables refuse changes. `meta.check_result_current` is the newest report.

```sql
SELECT layer, source_id, check_name, severity, bad, total
FROM meta.check_result_current WHERE severity <> 'ok' ORDER BY severity, layer;
```

## Measured on the real data
The sandbox holds NRW and Hamburg: 10.6 million current rows in 7 layers and 15 sources. The full
check, with the validity scan of every geometry, takes about 100 seconds and **passes: 0 errors,
20 warnings**. The warnings are findings about the data, not about the pipeline:

| layer | warning | meaning |
| --- | --- | --- |
| `osm_power` | voltage missing 11.0% (Hamburg) and 8.4% (NRW); output not parsable for 29.1% of the NRW rows | OSM tags are incomplete; the review of the substations (ADR-0023) deals with the voltage |
| `wind_unit` | no coordinate for 1,335 units (3.0%); 50 positions outside the envelope of Germany; 13 outside their state; 2 with a placeholder | the register is weak where operators entered their own coordinates; the wind core (ADR-0026) carries these as position classes |
| `protected_area` | one area each of the habitat and the nature reserve sources lies outside the envelope | to be looked at; the check cannot say why |
| `parcel` | official area and area of the outline differ for 13.9% of the NRW parcels (0.6% by a large amount) and 2.4% of the Hamburg parcels | the totals agree to 0.011% (NRW) and 0.08% (Hamburg), the single parcels differ (ADR-0018) |

Terrain is clean: 36,728 tiles, all in EPSG:25832. Geometries that had to be repaired (OSM and
protected areas) are flagged `geometry_repaired` (information), and no geometry is invalid after
the repair.

## The verdict of the wind unit register
`make validate SOURCES=mastr_bulk` judges every applied snapshot of the register: an empty state or
more than 20% of the units removed is an error; units without a coordinate (warning above 2%, error
above 30%), with a placeholder, outside the state or outside Germany (warning above 0.1%, error
above 5%) and without or with an implausible capacity (warning above 0.5%, error above 5%) are
judged by their share. `make windcore` requires a passed verdict for the register, as for the OSM
sources.
