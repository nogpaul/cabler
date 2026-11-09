# ADR-0016: Vector datasets in zips

Status: accepted · Date: 2026-11-09

## Context
Land cover comes from CORINE as one GeoPackage of 5.4 GB inside a zip (657,676 polygons); the
protected areas come from the BfN as six small GML files, each in its own zip. Both are read with
GDAL through `pyogrio`. Reading the GeoPackage straight from the compressed zip took 39 seconds for
the first 5,000 polygons and 240 seconds for 5,000 polygons from the middle of the file: a
GeoPackage is a database, and jumping around in a deflated stream means decompressing from the
start each time. The full read would have taken hours.

## Decision
- **One reader for all of them** (`cabler.ogr`). A *theme* names the member of the zip, the layer in
  it, the name under which the features are stored and the attribute that identifies a feature.
  A source's `[load]` section has `kind = "ogr"` and one theme; adding a vector source is a theme
  and a source file, not new code.
- **Small members are read straight from the zip. A member marked `unpack` is unpacked once** into
  a temporary folder, verified by the zip's own checksum, read in batches of 20,000 and deleted
  again, also when the read fails. The 5.4 GB member unpacks in under five minutes, and the whole
  load takes 5 minutes 20 seconds.
- **Attributes are stored as text, geometry as WKB with its EPSG number**, like every other
  non-OSM source (ADR-0013). A layer whose coordinate system is not an EPSG code is refused.
- **The identifier is the publisher's** where there is one (`gml_id` for the BfN files). CORINE has
  none, so the row number in the file is used; a repeated or missing identifier stops the load.

## Consequences
- The load needs scratch space of about the size of the largest unpacked member (5.4 GB in the
  system temporary folder).
- A new CORINE release re-keys every polygon, so its diff shows everything as changed; this is
  accepted for a dataset published every six years and is stated in the table comment.
- GML members are read in one piece; they are at most a few hundred MB.
