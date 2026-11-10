# ADR-0022: The catalogue reports what it cannot use, and the readers agree on their limits

Status: accepted · Date: 2026-11-10

## Context
The reviews of the terrain catalogue and of the parcel hardening found thirteen places where the
code did less than it said, or where two parts of it disagreed. One unreadable tile among 35,860
stopped the whole catalogue; two tiles of the same name in a zip crashed it; the overlap check
missed a tile that lies inside another; every run rewrote every row; a zip inside a zip had no
size limit; and one check followed symbolic links while another, in the same release, was
changed so that it did not.

## Decision
- **A tile that cannot be used is reported, and the others are catalogued.** A missing file, a
  size that differs from the one recorded at the fetch, or a header that cannot be described puts
  the tile into `unreadable` with its reason. The report names it and the exit code is 1. Checking
  every file's checksum again stays the job of `tiles --verify`.
- **A name that occurs twice in a zip** is one tile if the content is the same, and a refusal if
  it is not.
- **The zip inside a zip may not unpack to more than 8 GiB**, checked before it is unpacked. The
  vector reader checks the declared size of its member before either way of reading it.
- **Two tiles overlap when their intersection has an area.** A tile inside another, or one with
  the same corner, counts; tiles that only touch do not.
- **Only changed rows are written** when the current set is marked, and only a fetched or loaded
  snapshot is considered for a source that is one zip.
- **Corners are compared with a tolerance of a micrometre**, because coordinates come out of
  floating-point arithmetic.
- **Paths are judged by their form, not by the file system.** The tile store applies the rule of
  the index to the name; a path in a listing may not be absolute or contain `..`. A tile folder
  or a tile that is a link to another disk is therefore fine.
- **An XML part is streamed from the zip** instead of being read into memory first.

## Consequences
- Run on the real tiles of both states, the catalogue gives the same result as before: 36,728
  tiles, no findings, no overlaps, no holes.
- A catalogue run that reports unreadable tiles has stored the rest; the exit code tells the
  operator that something needs a look.
- The limits are constants of the modules; a source with larger members needs a deliberate change.
