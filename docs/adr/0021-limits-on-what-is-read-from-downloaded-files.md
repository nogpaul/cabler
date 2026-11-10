# ADR-0021: Limits on what is read from downloaded files

Status: accepted · Date: 2026-11-09

## Context
The reviews of the parcel and the tile-name releases found places where a downloaded file could
ask the reader for more than it should give, or where the reader gave less than it promised. A
surface with two patches was read as its first patch only; a zip member was unpacked without any
limit on its size; a tile listing could name a path outside the raw directory; and the arcs of
the Hamburg export lost up to 6.6 mm at real UTM coordinates, against a promise of 5 mm. The
Hamburg apply path, which differs from NRW's in its field names, had no test against a database.

## Decision
- **A file is refused when it asks for too much.** An XML part of an exchange file may not unpack
  to more than 1 GiB, and a zip member that must be unpacked first (a GeoPackage) not to more
  than 8 GiB. The size is the one declared in the zip's directory, which the zip reader
  enforces while reading, so it cannot be understated.
- **A shape that cannot be represented is refused, not shortened.** A surface with more than one
  patch, or a patch with more than one exterior ring, stops the load with the parcel's key.
- **Arcs are computed relative to their first point.** The squares of UTM coordinates (about
  10^13) cost precision before the circle is found; relative coordinates do not. A test with a
  radius of 1 m at real coordinates fails on the old code and passes on the new.
- **A tile path must stay inside the raw directory.** The listing of a snapshot is already
  compared with the checksum recorded in the database before anything is read; each path in it
  is now also checked. The store compares the folder of a target as written, not as resolved.
- **A parcel without a register area is a finding.** `area_missing` (migration 0015, a comment
  only), so a changed attribute name at the publisher cannot pass as a clean load.
- **The Hamburg path is tested against the database**, through fetch, load, diff and apply and a
  second release, and the close statement takes the layer from one function instead of a text
  replacement.

## Consequences
- The limits are constants of the reader modules; a state with larger parts needs a deliberate
  change, not a silent one.
- The same limits for the terrain catalogue (an inner zip) follow with its own hardening.
- Memory per XML part no longer grows with the number of parcels: a processed member is removed
  from its parent.
