# ADR-0018: Parcels: tile sets, official keys and two readers

Status: accepted · Date: 2026-11-09

## Context
Parcels (ALKIS, without owner data) come in two shapes. NRW publishes 53 GeoPackage zips, one per
district (7.4 GB, licence dl-de/zero-2-0); a new release may change any of them. Hamburg publishes
one zip of 289 XML parts in the ALKIS exchange format (NAS), quarterly (459 MB, licence
dl-de/by-2-0, with credit). GDAL has no NAS driver here, and its generic GML driver returns only the
label point of a Hamburg parcel, not its outline, which sits in a separate `position` property.
The stages after *fetch* (load, diff, apply) work on one snapshot per source, and an unchanged
source must not be loaded twice.

## Decision
- **A set of tiles becomes one snapshot.** When a tile source has a `[load]` section, the load stage
  first records the current tiles as a snapshot: a listing (`tiles.json`) of every tile with its
  checksum, in a snapshot folder with a manifest, and a row in `meta.source_version` whose checksum
  is the listing's. The same tiles give the same snapshot; a changed tile gives a new one. Each tile
  is checked against its recorded checksum before it is read. Diff, apply and history then work as
  for any source.
- **A parcel is identified by its official key** (`flstkennz` in NRW, `flurstueckskennzeichen` in
  Hamburg), not by its position in a file, so a different split of the files does not make
  every parcel look new. The key must be unique over the whole set; the database enforces it.
- **NRW is read by the vector reader** (ADR-0016): the member of each zip is found by the pattern
  `*.gpkg`, unpacked, and its `Flurstueck` layer read in batches.
- **Hamburg is read by its own reader** (`cabler.nas`): it streams each XML part from the zip, builds
  the outline from the curve segments (straight lines, and circular arcs replaced by segments
  within 5 mm), and keeps the attributes as text. It refuses an unknown coordinate system, a ring
  that is not closed, and a repeated key.
- **Both are staged into `staging.parcel`**, reprojected to EPSG:25832, with history. The register's
  area is kept as delivered next to the area of the outline. Invalid outlines are repaired and
  flagged, never dropped (ADR-0017). At most one area finding per parcel:
  `area_mismatch_large` (over 10 per cent and 25 m2) or `area_mismatch` (over 1 per cent and 1 m2).

## Consequences
- The first run: 9,387,373 parcels in NRW and 261,686 in Hamburg, 18 and 6.5 minutes to load, no
  outline needed repair. The outlines tile the land: in Herne their union equals their sum.
- The register area and the outline area agree in total to 0.011 per cent (NRW) and 0.08 per cent
  (Hamburg). They differ parcel by parcel for rounding, for the projection (the register reduces to
  the ellipsoid) and, in a few thousand cases, for real; the large finding lists those.
- Another state is a source file and, if its format is new, a reader; the staging does not change.
- Only parcels are read. The same files carry buildings and land use for later phases.
