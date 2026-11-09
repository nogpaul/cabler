# ADR-0019: Tile names are checked before anything is downloaded

Status: accepted · Date: 2026-11-09

## Context
A tile source lists its files in an index, and the fetcher stores each file under the name the
index gives. The first download of the NRW parcels stopped at a file called
`gru_vereinf_05117000_Mülheim_ad_Ruhr_EPSG25832_GeoPackage.zip`: the tile table accepted only
ASCII names, as a safety rule. The failure showed a second problem. That rule was the only check
that a name could not leave its folder (`../x`), and it ran in the database after the file had
already been moved into place.

## Decision
- **The index is checked first.** `cabler.tiles.is_safe_name` accepts letters of any language,
  digits, `.`, `_` and `-`, up to 200 characters, and never a leading dot. A name that is not
  accepted stops the run before a single file is downloaded. Names are no longer limited to ASCII.
- **The store checks again** that the target lies directly inside the tiles folder.
- **The database keeps a constraint** as a last line of defence, now written as what is forbidden
  (an empty name, a separator, a space, a query or fragment mark, a control character, a leading
  dot) so that it does not depend on the locale of the database.
- **Tests** cover names with umlauts, every kind of forbidden name, and the database refusing them.

## Consequences
- A publisher's file names with accents or umlauts work without any further change.
- A name with a space or a percent sign is refused; if a publisher uses one, the rule is changed
  on purpose, not worked around.
- Adding a source that ships its own index format must call `is_safe_name` on its names.
