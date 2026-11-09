"""The header of a GeoTIFF: size, pixel size, origin, coordinate system, data type.

The terrain catalogue needs to know where each tile is and what it holds, for tens of thousands of
tiles, without decoding any elevation. Everything is in the first kilobytes of the file, so this
reads only a prefix of it (a few kilobytes are enough for the files of the German state surveys,
whose tag values sit right behind the directory). No raster library is needed, and no pixel is
ever decoded.

Supported: classic TIFF, either byte order, with the GeoTIFF tags ModelPixelScale, ModelTiepoint and
GeoKeyDirectory. BigTIFF, rotated rasters (ModelTransformation) and files whose directory lies
beyond the prefix are refused with a clear error rather than guessed at.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

PREFIX = 65_536

# TIFF and GeoTIFF tags
IMAGE_WIDTH = 256
IMAGE_LENGTH = 257
BITS_PER_SAMPLE = 258
COMPRESSION = 259
SAMPLE_FORMAT = 339
MODEL_PIXEL_SCALE = 33550
MODEL_TIEPOINT = 33922
MODEL_TRANSFORMATION = 34264
GEO_KEY_DIRECTORY = 34735
GDAL_NODATA = 42113
# GeoKeys
RASTER_TYPE = 1025  # 1 = PixelIsArea, 2 = PixelIsPoint
PROJECTED_CS = 3072

# TIFF field types: code -> (struct format character, size in bytes)
TYPES = {
    1: ("B", 1),
    2: ("c", 1),
    3: ("H", 2),
    4: ("I", 4),
    5: ("I", 8),
    11: ("f", 4),
    12: ("d", 8),
}


class GeoTiffError(Exception):
    """The bytes are not a GeoTIFF this reader can describe."""


@dataclass(frozen=True)
class GeoTiff:
    """What the header says about one raster."""

    width: int
    height: int
    bits_per_sample: int
    sample_format: int  # 1 unsigned, 2 signed, 3 floating point
    compression: int
    pixel_x: float
    pixel_y: float
    origin_x: float  # the upper left corner of the raster
    origin_y: float
    epsg: int | None
    nodata: str | None

    @property
    def x_min(self) -> float:
        return self.origin_x

    @property
    def x_max(self) -> float:
        return self.origin_x + self.width * self.pixel_x

    @property
    def y_max(self) -> float:
        return self.origin_y

    @property
    def y_min(self) -> float:
        return self.origin_y - self.height * self.pixel_y


def _values(
    data: bytes, order: str, tag: int, kind: int, count: int, raw: bytes
) -> tuple[Any, ...]:
    """The values of one directory entry; ``raw`` is its four value-or-offset bytes."""
    if kind not in TYPES:
        msg = f"a tag of type {kind}"
        raise GeoTiffError(msg)
    fmt, size = TYPES[kind]
    total = size * count
    if total <= 4:
        block = raw[:total]
    else:
        (offset,) = struct.unpack(order + "I", raw)
        if offset + total > len(data):
            msg = f"tag {tag} lies beyond the first {len(data)} bytes"
            raise GeoTiffError(msg)
        block = data[offset : offset + total]
    if kind == 2:
        return (block.split(b"\0", 1)[0].decode("ascii", "replace"),)
    if kind == 5:  # rationals are pairs of unsigned integers
        numbers = struct.unpack(order + "I" * (2 * count), block)
        return tuple(numbers[i] / numbers[i + 1] for i in range(0, len(numbers), 2))
    return struct.unpack(order + fmt * count, block)


def _directory(data: bytes, where: str) -> tuple[str, dict[int, tuple[Any, ...]]]:
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        msg = f"{where}: not a TIFF file"
        raise GeoTiffError(msg)
    order = "<" if data[:2] == b"II" else ">"
    magic, first = struct.unpack(order + "HI", data[2:8])
    if magic == 43:
        msg = f"{where}: BigTIFF is not supported"
        raise GeoTiffError(msg)
    if magic != 42:
        msg = f"{where}: not a TIFF file"
        raise GeoTiffError(msg)
    if first + 2 > len(data):
        msg = f"{where}: the directory lies beyond the first {len(data)} bytes"
        raise GeoTiffError(msg)
    (count,) = struct.unpack(order + "H", data[first : first + 2])
    if first + 2 + 12 * count > len(data):
        msg = f"{where}: the directory lies beyond the first {len(data)} bytes"
        raise GeoTiffError(msg)
    tags: dict[int, tuple[Any, ...]] = {}
    for i in range(count):
        start = first + 2 + 12 * i
        tag, kind, n = struct.unpack(order + "HHI", data[start : start + 8])
        try:
            tags[tag] = _values(data, order, tag, kind, n, data[start + 8 : start + 12])
        except GeoTiffError as exc:
            msg = f"{where}: {exc}"
            raise GeoTiffError(msg) from exc
    return order, tags


def parse(data: bytes, where: str = "the file") -> GeoTiff:
    """Describe a GeoTIFF from the first bytes of the file."""
    _, tags = _directory(data, where)

    def need(tag: int, name: str) -> Any:
        if not tags.get(tag):
            msg = f"{where}: the header has no {name}"
            raise GeoTiffError(msg)
        return tags[tag][0]

    if MODEL_TRANSFORMATION in tags:
        msg = f"{where}: a rotated or sheared raster (ModelTransformation) is not supported"
        raise GeoTiffError(msg)
    scale = tags.get(MODEL_PIXEL_SCALE, ())
    tiepoint = tags.get(MODEL_TIEPOINT, ())
    if len(scale) < 2 or len(tiepoint) < 6:
        msg = f"{where}: the header has no ModelPixelScale and ModelTiepoint"
        raise GeoTiffError(msg)
    pixel_x, pixel_y = float(scale[0]), float(scale[1])
    # the tiepoint ties raster position (i, j) to the model position (x, y)
    i, j, x, y = (float(v) for v in (tiepoint[0], tiepoint[1], tiepoint[3], tiepoint[4]))

    epsg, raster_type = None, 1
    directory = tags.get(GEO_KEY_DIRECTORY, ())
    if len(directory) >= 4:
        for k in range(int(directory[3])):
            base = 4 + 4 * k
            if base + 3 >= len(directory):
                break
            key, location, _count, value = directory[base : base + 4]
            if location == 0 and key == PROJECTED_CS:
                epsg = int(value)
            if location == 0 and key == RASTER_TYPE:
                raster_type = int(value)
    origin_x = x - i * pixel_x
    origin_y = y + j * pixel_y
    if raster_type == 2:  # PixelIsPoint: the tiepoint is the centre of the first pixel
        origin_x -= pixel_x / 2
        origin_y += pixel_y / 2
    nodata = tags.get(GDAL_NODATA)
    return GeoTiff(
        width=int(need(IMAGE_WIDTH, "ImageWidth")),
        height=int(need(IMAGE_LENGTH, "ImageLength")),
        bits_per_sample=int(need(BITS_PER_SAMPLE, "BitsPerSample")),
        sample_format=int(tags[SAMPLE_FORMAT][0]) if tags.get(SAMPLE_FORMAT) else 1,
        compression=int(need(COMPRESSION, "Compression")),
        pixel_x=pixel_x,
        pixel_y=pixel_y,
        origin_x=origin_x,
        origin_y=origin_y,
        epsg=epsg,
        nodata=str(nodata[0]).strip() if nodata else None,
    )
