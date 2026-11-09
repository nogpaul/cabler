import struct

import pytest

from cabler import geotiff
from cabler.geotiff import GeoTiffError

SHORT, LONG, DOUBLE, ASCII = 3, 4, 12, 2
FORMATS = {SHORT: "H", LONG: "I", DOUBLE: "d"}


def make_tiff(
    *,
    order: str = "<",
    width: int = 1000,
    height: int = 1000,
    origin: tuple[float, float] = (280000.0, 5653000.0),
    pixel: tuple[float, float] = (1.0, 1.0),
    epsg: int | None = 25832,
    raster_type: int = 1,
    nodata: str | None = "-9999",
    bits: int = 32,
    sample_format: int | None = 3,
    compression: int = 5,
    drop: tuple[int, ...] = (),
    extra: dict[int, tuple[int, list[float]]] | None = None,
) -> bytes:
    keys = [(1025, 0, 1, raster_type)]
    if epsg is not None:
        keys.append((3072, 0, 1, epsg))
    directory = [1, 1, 0, len(keys), *[v for key in keys for v in key]]
    entries: dict[int, tuple[int, list[object]]] = {
        256: (SHORT, [width]),
        257: (SHORT, [height]),
        258: (SHORT, [bits]),
        259: (SHORT, [compression]),
        33550: (DOUBLE, [pixel[0], pixel[1], 0.0]),
        33922: (DOUBLE, [0.0, 0.0, 0.0, origin[0], origin[1], 0.0]),
        34735: (SHORT, directory),
    }
    if sample_format is not None:
        entries[339] = (SHORT, [sample_format])
    if nodata is not None:
        entries[42113] = (ASCII, [nodata])
    for tag, value in (extra or {}).items():
        entries[tag] = value  # type: ignore[assignment]
    for tag in drop:
        entries.pop(tag, None)

    count = len(entries)
    data_at = 8 + 2 + 12 * count + 4
    body = b""
    rows = b""
    for tag in sorted(entries):
        kind, values = entries[tag]
        if kind == ASCII:
            raw = str(values[0]).encode("ascii") + b"\0"
            n = len(raw)
        else:
            raw = struct.pack(order + FORMATS[kind] * len(values), *values)
            n = len(values)
        if len(raw) <= 4:
            value_field = raw.ljust(4, b"\0")
        else:
            value_field = struct.pack(order + "I", data_at + len(body))
            body += raw
        rows += struct.pack(order + "HHI", tag, kind, n) + value_field
    header = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, 8)
    return header + struct.pack(order + "H", count) + rows + struct.pack(order + "I", 0) + body


@pytest.mark.parametrize("order", ["<", ">"])
def test_the_header_of_a_tile_is_described_in_either_byte_order(order: str) -> None:
    info = geotiff.parse(make_tiff(order=order))
    assert (info.width, info.height) == (1000, 1000)
    assert (info.pixel_x, info.pixel_y) == (1.0, 1.0)
    assert (info.x_min, info.y_max, info.x_max, info.y_min) == (280000, 5653000, 281000, 5652000)
    assert info.epsg == 25832
    assert (info.bits_per_sample, info.sample_format, info.compression) == (32, 3, 5)
    assert info.nodata == "-9999"


def test_a_pixel_that_is_a_point_shifts_the_extent_by_half_a_pixel() -> None:
    info = geotiff.parse(make_tiff(raster_type=2, origin=(280000.5, 5652999.5)))
    assert (info.x_min, info.y_max) == (280000.0, 5653000.0)


def test_missing_optional_things_are_none_or_defaults() -> None:
    info = geotiff.parse(make_tiff(nodata=None, epsg=None, sample_format=None))
    assert info.nodata is None
    assert info.epsg is None
    assert info.sample_format == 1


def test_a_header_beyond_the_prefix_is_refused_rather_than_guessed() -> None:
    data = make_tiff()
    with pytest.raises(GeoTiffError, match="lies beyond"):
        geotiff.parse(data[:60], "tile.tif")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "not a TIFF"),
        (b"PK\x03\x04" + b"\0" * 20, "not a TIFF"),
        (b"II" + struct.pack("<HI", 43, 8) + b"\0" * 20, "BigTIFF"),
        (b"II" + struct.pack("<HI", 41, 8) + b"\0" * 20, "not a TIFF"),
    ],
)
def test_something_that_is_not_a_classic_tiff_is_refused(data: bytes, message: str) -> None:
    with pytest.raises(GeoTiffError, match=message):
        geotiff.parse(data)


def test_a_raster_without_its_georeferencing_is_refused() -> None:
    with pytest.raises(GeoTiffError, match="ModelPixelScale"):
        geotiff.parse(make_tiff(drop=(33550,)))
    with pytest.raises(GeoTiffError, match="ImageWidth"):
        geotiff.parse(make_tiff(drop=(256,)))


def test_a_rotated_raster_is_refused() -> None:
    rotated = make_tiff(extra={34264: (DOUBLE, [1.0] * 16)})
    with pytest.raises(GeoTiffError, match="ModelTransformation"):
        geotiff.parse(rotated)


def test_a_tag_of_an_unknown_type_is_refused() -> None:
    odd = make_tiff(extra={50000: (LONG, [1])})
    patched = bytearray(odd)
    # find the entry of tag 50000 and change its type to 99
    index = bytes(patched).find(struct.pack("<H", 50000))
    patched[index + 2 : index + 4] = struct.pack("<H", 99)
    with pytest.raises(GeoTiffError, match="type 99"):
        geotiff.parse(bytes(patched))
