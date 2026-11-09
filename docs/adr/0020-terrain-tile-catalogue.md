# ADR-0020: The terrain tile catalogue

Status: accepted · Date: 2026-11-09

## Context
Both states publish their 1 m terrain model as GeoTIFF tiles: NRW as 35,860 files fetched one by
one, Hamburg as 868 files in a zip inside a zip. Everything downstream (the slope and cost
surface, the route search) needs to know where each tile is, whether the set is complete and
whether all tiles are alike, without opening 36,728 rasters or decoding their pixels.

## Decision
- **The header is enough.** `cabler.geotiff` reads the first 64 kB of a file and returns size,
  pixel size, origin, coordinate system, data type, compression and no-data value. It needs no
  raster library, decodes nothing and refuses what it cannot describe (BigTIFF, rotated rasters, a
  directory beyond the prefix). It agrees with `gdalinfo` on real tiles of both states.
- **`staging.terrain_tile` is the catalogue**: one row per tile file (source, name, SHA-256 of the
  file), with its footprint as a polygon in EPSG:25832, its properties and findings
  (`qa_flags`). A replaced tile is a new row; `is_current` marks the set catalogued last. The
  catalogue is derived and can be rebuilt from the files at any time.
- **Findings per tile:** a coordinate system other than EPSG:25832, a pixel size other than 1 m, a
  size other than 1000 x 1000, a corner off the 1 km grid, a name that disagrees with the extent,
  no no-data value, or samples that are not floating point.
- **Checks of the set:** duplicate corners, overlapping footprints and holes (a missing tile with
  tiles on every side), from the footprints in the database.
- **Where the tiles come from:** a tile source is catalogued from `meta.tile`; any other source is
  read from its snapshot zip, and a zip inside the zip is unpacked once into a temporary folder.
  A source says `[catalogue] kind = "geotiff"`; the stage is `catalogue` in the run history.

## Consequences
- The first run over both states took 1.5 minutes: 36,728 tiles, no findings, no overlaps, no
  gaps inside the coverage.
- Elevation statistics (minimum, maximum, share of no-data) need the pixels and are left to the
  cost-surface phase, where a raster library is needed anyway.
- A third state's tiles are catalogued by adding a source file, with the same checks.
