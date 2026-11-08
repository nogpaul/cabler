"""The tile fetch against a real database with a simulated publisher and a temporary data folder.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path

import httpx
import pytest

from cabler import db, remote, sources, tiles

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

BASE = "https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/"


class Publisher:
    """An index plus its tiles; change `files` to simulate a new release."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {
            "dgm1_32_280_5652_1_nw_2022.tif": (b"a" * 100, "2023-05-22T08:43:46"),
            "dgm1_32_280_5653_1_nw_2022.tif": (b"b" * 120, "2023-05-22T08:44:46"),
            "dgm1_32_281_5652_1_nw_2022.tif": (b"c" * 80, "2023-05-22T08:45:46"),
        }
        self.broken: set[str] = set()
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def index(self) -> bytes:
        rows = "".join(
            f'<file name="{name}" size="{len(body)}" timestamp="{stamp}" />'
            for name, (body, stamp) in sorted(self.files.items())
        )
        xml = (
            "<opengeodata><datasets><dataset><files>"
            f"{rows}</files></dataset></datasets></opengeodata>"
        )
        return xml.encode()

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "":
            body = self.index()
        elif name in self.broken:
            body = b"short"
        else:
            body = self.files[name][0]
        return httpx.Response(
            200, stream=httpx.ByteStream(body), headers={"Content-Length": str(len(body))}
        )


def make_spec() -> sources.SourceSpec:
    base = sources.load_all()["osm_hamburg"]
    return dataclasses.replace(
        base,
        source_id="itest_source",
        url=BASE,
        allowed_hosts=("www.opengeodata.nrw.de",),
        max_bytes=10_000,
        file_format="tif",
        load_kind=None,
        load_themes=(),
        tiles_kind="opengeodata_index",
    )


def test_a_first_run_fetches_every_tile_and_a_second_run_does_nothing(tmp_path: Path) -> None:
    spec, publisher = make_spec(), Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        plan_only = tiles.fetch_tiles(conn, publisher.client, spec, tmp_path)
        assert plan_only.status == "planned"
        assert (plan_only.listed, plan_only.new, plan_only.planned_bytes) == (3, 3, 300)
        assert not (tmp_path / "itest_source" / "tiles").exists()

        seen: list[tuple[int, int, int]] = []
        first = tiles.fetch_tiles(
            conn,
            publisher.client,
            spec,
            tmp_path,
            yes=True,
            workers=2,
            on_progress=lambda done, total, got: seen.append((done, total, got)),
        )
        assert (first.status, first.fetched, first.failed, first.bytes_fetched) == (
            "fetched",
            3,
            0,
            300,
        )
        assert seen[-1] == (3, 3, 300)
        folder = tmp_path / "itest_source" / "tiles"
        assert sorted(p.name for p in folder.iterdir()) == sorted(publisher.files)
        assert all(p.stat().st_mode & 0o222 == 0 for p in folder.iterdir())
        rows = conn.execute(
            "SELECT status, count(*), sum(size) FROM meta.tile"
            " WHERE source_id = %s GROUP BY status",
            (spec.source_id,),
        ).fetchall()
        assert rows == [("fetched", 3, 300)]

        second = tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        assert (second.status, second.unchanged, second.fetched) == ("up_to_date", 3, 0)


def test_a_changed_tile_keeps_its_old_file_and_a_vanished_tile_is_marked(tmp_path: Path) -> None:
    spec, publisher = make_spec(), Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)

        publisher.files["dgm1_32_280_5652_1_nw_2022.tif"] = (b"z" * 150, "2024-01-01T00:00:00")
        del publisher.files["dgm1_32_281_5652_1_nw_2022.tif"]
        result = tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        assert (result.changed, result.removed, result.fetched) == (1, 1, 1)

        folder = tmp_path / "itest_source" / "tiles"
        assert (folder / "dgm1_32_280_5652_1_nw_2022.tif").read_bytes() == b"z" * 150
        (old,) = (folder / "superseded").iterdir()
        assert old.read_bytes() == b"a" * 100
        states = conn.execute(
            "SELECT name, status FROM meta.tile WHERE source_id = %s"
            " ORDER BY name, remote_timestamp",
            (spec.source_id,),
        ).fetchall()
        assert states == [
            ("dgm1_32_280_5652_1_nw_2022.tif", "superseded"),
            ("dgm1_32_280_5652_1_nw_2022.tif", "fetched"),
            ("dgm1_32_280_5653_1_nw_2022.tif", "fetched"),
            ("dgm1_32_281_5652_1_nw_2022.tif", "removed"),
        ]


def test_a_broken_tile_fails_alone_and_is_picked_up_by_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tiles.time, "sleep", lambda seconds: None)
    spec, publisher = make_spec(), Publisher()
    publisher.broken.add("dgm1_32_280_5653_1_nw_2022.tif")
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        assert (first.fetched, first.failed) == (2, 1)
        assert "dgm1_32_280_5653_1_nw_2022.tif" in first.failures[0]

        publisher.broken.clear()
        second = tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True)
        assert (second.new, second.fetched, second.unchanged, second.failed) == (1, 1, 2, 0)


def test_the_limit_fetches_only_the_first_missing_tiles(tmp_path: Path) -> None:
    spec, publisher = make_spec(), Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        result = tiles.fetch_tiles(conn, publisher.client, spec, tmp_path, yes=True, limit=1)
        assert (result.new, result.fetched) == (3, 1)
        assert result.planned_bytes == 100


def test_the_tile_table_is_readable_by_the_read_only_role() -> None:
    with db.connect("ro") as conn:
        assert conn.execute("SELECT count(*) FROM meta.tile").fetchone() is not None
