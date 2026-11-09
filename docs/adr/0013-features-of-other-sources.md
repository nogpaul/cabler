# ADR-0013: Features of sources that are not OpenStreetMap

Status: accepted · Date: 2026-11-09

## Context
The wind units come from the Marktstammdatenregister (an XML export of 3.3 GB inside a zip, text
in UTF-16), land cover from CORINE (a GeoPackage inside a zip) and protected areas from the BfN
(GML). Each has its own columns, but all of them are "features with attributes and perhaps a
geometry", each with an identifier the publisher keeps stable. The OSM tables fit OSM only.

## Decision
- **One generic table, `raw.feature`**, holds a feature per loaded snapshot: `layer`,
  `feature_key` (the publisher's identifier), `attributes` (jsonb, every value as delivered, text),
  `geom` (the coordinate system of the delivery is part of the value) and `content_hash`
  (SHA-256 over the attributes, the coordinate system and the geometry). A change set,
  `raw.feature_change`, mirrors `raw.osm_change`: added, changed (with the names of the attributes
  that differ, and `geometry`) or removed.
- **Typing and decoding happen on the way to staging**, never in raw, so raw always shows what the
  publisher delivered.
- **The MaStR export is read as a stream.** The zip is never unpacked (it expands to about 67 GB);
  the UTF-16 XML members are parsed with `defusedxml`'s incremental parser and each element is
  released when read. A feature without an identifier, or a repeated one, stops the load with an
  error; a silent merge would hide a data problem.
- **A source's `[load]` section names its reader** (`kind`) and the themes to read. The `load`,
  `diff` and `apply` stages pick the implementation from the kind.

## Consequences
- A new non-OSM source needs a reader and a staging table, not a new raw schema.
- Attributes are not queryable with column types until staged; jsonb expressions are used for
  inspection only.
- Features are stored per snapshot in full, so an unchanged feature costs a row per snapshot.
  This is accepted for the sizes involved (46,000 rows for MaStR wind) and is the reason the
  hash exists; a source with millions of features per snapshot may later store only changes.
