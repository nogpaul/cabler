# ADR-0017: Land cover and protected areas in staging

Status: accepted · Date: 2026-11-09

## Context
Routing needs two polygon layers beside the grid: what the land is (CORINE: 657,676 polygons for
Germany, 44 classes) and where building is restricted or forbidden (BfN: nature reserves, habitat
and bird areas, national parks and landscape areas). Both come as published, with invalid
polygons among them, and neither says which state a polygon is in beyond the BfN's two-letter code.

## Decision
- **`staging.land_cover` and `staging.protected_area`** keep one row per version of a polygon, with
  the history convention of the other staging tables, reprojected to EPSG:25832 and stored as
  MultiPolygon. All of Germany is stored, not only the study states; a state is selected by a
  query.
- **Invalid geometry is repaired and flagged**, never dropped: `ST_MakeValid`, then only the
  polygon parts are kept (`geometry_repaired`). A missing or empty geometry is kept and flagged
  (`geometry_missing`, `geometry_empty`). In the first run 187 of 19,060 protected areas needed a
  repair; no CORINE polygon did.
