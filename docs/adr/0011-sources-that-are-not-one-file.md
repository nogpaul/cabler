# ADR-0011: Sources that are not a single file

Status: accepted · Date: 2026-11-08

## Context
The pipeline was built around one URL per source, answering HEAD with validators (ADR-0007). Real
publishers differ: a web feature service (WFS) builds its file on request and rejects HEAD; a state
publishes its terrain model as 35,860 tiles beside an index; one federal service fails when asked
for a whole layer (about 4,000 features is its limit); redirect hops may lead to the `www` alias of
the same host; a slow service can be silent for a minute before the first byte.

## Decision
- **`probe = "none"`** in `[download]` marks a source that cannot be asked cheaply. The check says
  "unverifiable" and each fetch downloads; identical content is still stored once (SHA-256).
- **`[tiles] kind = "opengeodata_index"`** marks an index source. `cabler.tiles` reads the index,
  sorts tiles into new, changed, unchanged and removed against `meta.tile`, downloads the new and
  changed ones in parallel (default 6), verifies each against the size in the index, and records
  each at once, so an interrupted run resumes. A changed tile keeps its old file under
  `superseded/`; nothing is deleted. Without `--yes` only the plan is shown.
- **Areas of interest** are expressed in the source file, for example a bounding box in EPSG:25832
  in the WFS request, one source per state. Adding a state is a new file, not new code.
- **Transport failures** (timeout, reset, DNS) become `ConnectionFailedError`; a connection that
  breaks mid-download is a `DownloadError`. Both are reported per source, and the other sources
  still run. The read timeout is 180 seconds.
- **Allow-lists stay strict**: a redirect to another host is refused until that host is named in
  `allowed_hosts` (the `www` alias of one operator needed this).
- **Untrusted XML** is parsed with `defusedxml`.
- **Output formats** are taken from what the service actually delivers, not what its capabilities
  document advertises: the BfN service lists GeoPackage and Shapefile but answers 400 for them,
  so GML 3.2 in a zip is used.

## Consequences
- Whole-state terrain (78.8 GB for NRW) can be fetched, verified and resumed with the same audit
  trail as any other source.
- A source that cannot be probed is re-downloaded on every fetch; this is accepted for small
  services and avoided for large ones by using an index.
- Each new publisher quirk becomes a documented setting rather than a special case in code.
