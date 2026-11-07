# ADR-0004: ETRS89 / UTM 32N, vectors in PostGIS, rasters as Cloud Optimized GeoTIFF

Status: accepted · Date: 2026-11-07

## Decision
- All stored geometry uses ETRS89 / UTM 32N (EPSG:25832), the national standard for German
  cadastral and topographic data. Inputs in other systems are reprojected at the staging boundary
  and the original system is recorded.
- Vectors live in PostGIS with geometry type and SRID constraints.
- Rasters (cost surfaces, elevation) are stored as tiled Cloud Optimized GeoTIFF files outside the
  database, with checksums recorded in `meta`.
- Raster processing is tile-based with overlap, so memory use stays bounded as the study area grows.

## Consequences
- Eastern Germany lies partly in UTM zone 33; the distortion this causes is quantified before the
  cost model relies on it.
- Data files are never committed to git.
