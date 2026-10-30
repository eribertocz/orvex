"""P1-13: `filter: auto` loads only newer rows when the filter is proven, and refuses otherwise."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = (
    "version: 1\nconnections:\n"
    "  feed:\n    type: parquet\n    path: data/feed.parquet\n"
    "  registry:\n    type: parquet\n    path: data/consumers.parquet\n"
)
AUTO = (
    "version: 1\nmaterialized: incremental\nincremental: {strategy: append, time_column: read_at}\n"
)


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def feed(folder: Path, *rows: tuple[int, str, str]) -> None:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    values = ", ".join(f"({i}, '{c}', '{at}')" for i, c, at in rows)
    connection = duckdb.connect()
    connection.execute(
        "COPY (SELECT id::BIGINT AS id, consumer_id, read_at::TIMESTAMPTZ AS read_at "
        f"FROM (VALUES {values}) AS t(id, consumer_id, read_at)) "
        f"TO '{folder / 'data' / 'feed.parquet'}'"
    )
    connection.execute(
        "COPY (SELECT * FROM (VALUES ('C1', 'a'), ('C2', 'b')) AS t(consumer_id, tariff)) "
        f"TO '{folder / 'data' / 'consumers.parquet'}'"
    )


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def ids(folder: Path) -> list[object]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    found = [row[0] for row in lake.execute("SELECT id FROM lake.s.m ORDER BY id").fetchall()]
    lake.close()
    return found


JOINED = (
    "SELECT f.id, f.read_at, c.tariff FROM feed.data AS f "
    "JOIN registry.data AS c ON f.consumer_id = c.consumer_id"
)


# An append model may not join (#194, decision #217 A): the joined model is a merge model.
MERGE = AUTO.replace("strategy: append", "strategy: merge, unique_key: [id]")


@pytest.mark.parametrize(
    ("sql", "sidecar"),
    [("SELECT * FROM feed.data", AUTO), (JOINED, MERGE)],
    ids=["append", "joined-merge"],
)
def test_a_proven_filter_loads_only_newer_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sql: str, sidecar: str
) -> None:
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": sql,
            "models/s/m.yaml": sidecar,
        },
    )
    original = (tmp_path / "models" / "s" / "m.sql").read_bytes()
    feed(tmp_path, (1, "C1", "2024-01-01 10:00:00+00"), (2, "C2", "2024-01-02 10:00:00+00"))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    # The same rows plus a newer one: only the newer one is loaded (no duplicates).
    feed(
        tmp_path,
        (1, "C1", "2024-01-01 10:00:00+00"),
        (2, "C2", "2024-01-02 10:00:00+00"),
        (3, "C1", "2024-01-03 10:00:00+00"),
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 1 rows" in out
    assert ids(tmp_path) == [1, 2, 3]
    code, out = command(capsys, "validate", str(tmp_path), "--show-sql", "s.m")
    assert code == 0
    assert "> $since" in out
    assert (tmp_path / "models" / "s" / "m.sql").read_bytes() == original


def test_an_append_model_that_joins_is_refused_before_anything_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #194: a reading whose consumer arrives after the run would never be loaded.
    feed(tmp_path, (1, "C1", "2024-01-01 10:00:00+00"))
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": JOINED,
            "models/s/m.yaml": AUTO,
        },
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert out.startswith("INVALID_MODEL_SQL models/s/m.sql ")
    assert "no join of any kind" in out
    assert not (tmp_path / f".{SLUG}").exists()


def test_an_unprovable_filter_is_refused_with_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feed(tmp_path, (1, "C1", "2024-01-01 10:00:00+00"))
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": (
                "SELECT consumer_id,\n  max(read_at) AS read_at\nFROM feed.data\n"
                "GROUP BY consumer_id"
            ),
            "models/s/m.yaml": AUTO,
        },
    )
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("INCREMENTAL_FILTER_NOT_PROVABLE models/s/m.sql:2 ")
    assert "reason: aggregation" in out
    assert not (tmp_path / f".{SLUG}").exists()


def test_since_in_the_sql_needs_filter_manual(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feed(tmp_path, (1, "C1", "2024-01-01 10:00:00+00"))
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": "SELECT * FROM feed.data WHERE read_at > $since",
            "models/s/m.yaml": AUTO,
        },
    )
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("MODEL_CONFIG_INVALID models/s/m.yaml")
