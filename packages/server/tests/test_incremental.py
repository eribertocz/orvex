"""P1-11, P1-12, P1-14: incremental models — strategies, schema changes, `$since` and watermarks."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
MODEL = "SELECT * FROM feed.data"


def with_since(sql: str, append: bool) -> str:
    """The model `s.m` reading every row on each run: these tests are about how loaded rows are
    written, not which rows are loaded (`$since` is required in an incremental model's SQL). An
    `append` model may only read the rows after the watermark (#135); its tests feed new rows
    with later times."""
    if append:
        return f"SELECT * FROM ({sql}) WHERE read_at > $since"
    return f"SELECT * FROM ({sql}) WHERE $since IS NOT NULL"


def write(folder: Path, files: dict[str, str]) -> None:
    sidecar = files.get("models/s/m.yaml")
    if sidecar is None and (folder / "models" / "s" / "m.yaml").exists():
        sidecar = (folder / "models" / "s" / "m.yaml").read_text(encoding="utf-8")
    append = sidecar is not None and "strategy: append" in sidecar
    for relative, text in files.items():
        if relative == "models/s/m.sql" and "$since" not in text:
            text = with_since(text, append)
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def feed(folder: Path, rows: str) -> None:
    """Replace the source rows: id, region, kwh, big, read_at (TIMESTAMPTZ).

    Parquet has no 128-bit integer (DuckDB would write a HUGEINT as DOUBLE), so the widest exact
    integer of a Parquet source is DECIMAL(38,0)."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        "COPY (SELECT id::BIGINT AS id, region::VARCHAR AS region, kwh::DECIMAL(18,4) AS kwh, "
        "big::DECIMAL(38,0) AS big, read_at::TIMESTAMPTZ AS read_at "
        f"FROM (VALUES {rows}) AS t(id, region, kwh, big, read_at)) "
        f"TO '{folder / 'data' / 'feed.parquet'}'"
    )


def project(folder: Path, sidecar: str, model: str = MODEL) -> None:
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": model,
            "models/s/m.yaml": sidecar,
        },
    )


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def table(folder: Path) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    rows = lake.execute(
        "SELECT id, region, CAST(kwh AS VARCHAR), CAST(big AS VARCHAR), "
        "CAST(epoch_us(read_at) AS VARCHAR) FROM lake.s.m ORDER BY id, region"
    ).fetchall()
    lake.close()
    return rows


APPEND = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
)
MERGE = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: merge, time_column: read_at, unique_key: [id], filter: manual}\n"
)
BIG = "99999999999999999999999999999999999999"


def rows(*items: tuple[int, str, str]) -> str:
    """Rows whose read_at is day id % 9 + 1 of January 2024, 10:00 UTC. Numbers are given as
    text so DuckDB converts them exactly (a bare 39-digit literal would be read as DOUBLE)."""
    return ", ".join(
        f"({i}, '{region}', '{kwh}', '{BIG}', '2024-01-0{i % 9 + 1} 10:00:00+00')"
        for i, region, kwh in items
    )


def test_append_adds_loaded_rows_and_keeps_them_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, rows((1, "n", "1.0001"), (2, "n", "2.0002"), (3, "s", "3.0003")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    feed(tmp_path, rows((4, "n", "4.0004"), (5, "s", "5.0005")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 2 rows" in out
    assert [row[0] for row in table(tmp_path)] == [1, 2, 3, 4, 5]
    # Exact values survive (DECIMAL, a 38-digit DECIMAL(38,0), TIMESTAMPTZ as text).
    assert table(tmp_path)[0][2:] == (
        "1.0001",
        BIG,
        "1704189600000000",
    )
    write(
        tmp_path, {"models/s/m.sql": "SELECT * REPLACE (CAST('x' AS INTEGER) AS id) FROM feed.data"}
    )
    feed(tmp_path, rows((6, "n", "6.0006")))  # a later row: append reads only those (#135)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert len(table(tmp_path)) == 5


def test_merge_updates_matched_keys_and_inserts_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, MERGE)
    feed(tmp_path, rows((1, "n", "1.0000"), (2, "n", "2.0000"), (3, "s", "3.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, rows((2, "n", "2.5001"), (4, "s", "4.0000")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 2 rows" in out
    assert [(row[0], row[2]) for row in table(tmp_path)] == [
        (1, "1.0000"),
        (2, "2.5001"),
        (3, "3.0000"),
        (4, "4.0000"),
    ]


def test_merge_with_a_composite_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MERGE.replace("unique_key: [id]", "unique_key: [id, region]"))
    feed(tmp_path, rows((1, "n", "1.0000"), (1, "s", "2.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, rows((1, "s", "9.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    assert [(row[0], row[1], row[2]) for row in table(tmp_path)] == [
        (1, "n", "1.0000"),
        (1, "s", "9.0000"),
    ]


@pytest.mark.parametrize(
    ("loaded", "code"),
    [
        ("(NULL, 'n', 1, 1, '2024-01-01 00:00:00+00')", "UNIQUE_KEY_NULL"),
        (
            "(7, 'n', 1, 1, '2024-01-01 00:00:00+00'), (7, 's', 2, 1, '2024-01-01 00:00:00+00')",
            "UNIQUE_KEY_DUPLICATED",
        ),
    ],
)
def test_merge_refuses_null_and_duplicated_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], loaded: str, code: str
) -> None:
    project(tmp_path, MERGE)
    feed(tmp_path, rows((1, "n", "1.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, loaded)
    exit_code, out = command(capsys, "run", str(tmp_path))
    assert exit_code == 1
    assert out.splitlines()[-1].startswith(f"{code} ")
    assert [row[0] for row in table(tmp_path)] == [1]


def test_schema_change_fails_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, rows((1, "n", "1.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    for changed in [
        "SELECT *, 1 AS extra FROM feed.data",
        "SELECT id, region AS area, kwh, big, read_at FROM feed.data",
        "SELECT id, region, CAST(kwh AS DOUBLE) AS kwh, big, read_at FROM feed.data",
    ]:
        write(tmp_path, {"models/s/m.sql": changed})
        code, out = command(capsys, "run", str(tmp_path))
        assert code == 1, changed
        assert "SCHEMA_CHANGED" in out
        assert len(table(tmp_path)) == 1


# --- validation ---------------------------------------------------------------------------------


def test_sidecar_rules_need_no_lake(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MERGE.replace(", unique_key: [id]", ""))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("MISSING_UNIQUE_KEY models/s/m.yaml")
    project(tmp_path, APPEND.replace("time_column: read_at", "time_column: read_at, lookback: 1h"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert out.startswith("LOOKBACK_REQUIRES_UNIQUE_KEY models/s/m.yaml")
    assert not (tmp_path / f".{SLUG}").exists()


def test_column_rules_are_checked_by_binding_without_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feed(tmp_path, rows((1, "n", "1.0000")))
    # merge, not append: an append model's `$since` must bound its time column (#135), which
    # a VARCHAR column cannot be compared with.
    project(tmp_path, MERGE.replace("time_column: read_at", "time_column: region"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("TIME_COLUMN_NOT_TEMPORAL models/s/m.yaml")
    project(tmp_path, MERGE.replace("unique_key: [id]", "unique_key: [nothing]"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("MODEL_CONFIG_INVALID models/s/m.yaml")
    assert not (tmp_path / f".{SLUG}").exists()


def lake_value(folder: Path, sql: str) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    result = lake.execute(sql).fetchall()
    lake.close()
    return result


@pytest.mark.parametrize(
    ("sidecar", "expected"),
    [
        (
            APPEND,
            [
                (1, "n", "1.0000", BIG, "1704189600000000"),
                (2, "n", "2.0000", BIG, "1704276000000000"),
                # (2, "s") has the watermark's time: an append model reads only later rows
                # (#135).
                (3, "s", "3.0000", BIG, "1704362400000000"),
            ],
        ),
        (
            MERGE,
            # Every column of a matched key is replaced, not only the ones that changed.
            [
                (1, "n", "1.0000", BIG, "1704189600000000"),
                (2, "s", "2.5001", BIG, "1704276000000000"),
                (3, "s", "3.0000", BIG, "1704362400000000"),
            ],
        ),
    ],
)
def test_hugeint_survives_append_and_merge_exactly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    sidecar: str,
    expected: list[tuple[object, ...]],
) -> None:
    """Every value written by the second run goes through INSERT … SELECT from the stage."""
    hugeint = "SELECT id, region, kwh, CAST(big AS HUGEINT) AS big, read_at FROM feed.data"
    project(tmp_path, sidecar, hugeint)
    feed(tmp_path, rows((1, "n", "1.0000"), (2, "n", "2.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, rows((2, "s", "2.5001"), (3, "s", "3.0000")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert lake_value(tmp_path, "SELECT DISTINCT typeof(big) FROM lake.s.m") == [("HUGEINT",)]
    assert table(tmp_path) == expected


@pytest.mark.parametrize("sidecar", [APPEND, MERGE])
def test_a_later_failure_rolls_back_what_the_incremental_model_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sidecar: str
) -> None:
    project(tmp_path, sidecar)
    feed(tmp_path, rows((1, "n", "1.0000"), (2, "n", "2.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    before = table(tmp_path)
    # s.later runs after s.m (it reads it) and fails on the data, after s.m already wrote.
    write(tmp_path, {"models/s/later.sql": "SELECT CAST(region || 'x' AS INTEGER) AS n FROM s.m"})
    feed(tmp_path, rows((2, "s", "2.5001"), (3, "s", "3.0000")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "s.later" in out
    assert table(tmp_path) == before
    assert lake_value(
        tmp_path,
        "SELECT count(*) FROM duckdb_schemas() "
        "WHERE database_name = 'lake' AND schema_name = '_stage'",
    ) == [(0,)]


# --- P1-12: delete_insert, partition_overwrite, on_schema_change ---------------------------------

DELETE_INSERT = MERGE.replace("merge", "delete_insert")
PARTITION = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: partition_overwrite, time_column: read_at, partition_by: [month], "
    "filter: manual}\n"
)
# Every row on each run, with `$since` in a lower bound of the time column (#155: the only
# place a partition_overwrite model's `$since` may be).
BY_MONTH = (
    "SELECT id, region, kwh, strftime(read_at, '%Y-%m') AS month, read_at FROM feed.data "
    "WHERE read_at > $since - INTERVAL 100 YEAR OR read_at IS NULL"
)


def test_delete_insert_replaces_every_row_of_a_loaded_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The key is `region`: A ("n") has 2 rows and B ("s") 1; the second run loads 3 rows of A.
    project(tmp_path, DELETE_INSERT.replace("[id]", "[region]"))
    feed(tmp_path, rows((1, "n", "1.0000"), (2, "n", "2.0000"), (3, "s", "3.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, rows((4, "n", "4.0001"), (5, "n", "5.0002"), (6, "n", "6.0003")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 3 rows" in out  # rows inserted; the 2 deleted rows are not counted
    assert [(row[0], row[1], row[2]) for row in table(tmp_path)] == [
        (3, "s", "3.0000"),
        (4, "n", "4.0001"),
        (5, "n", "5.0002"),
        (6, "n", "6.0003"),
    ]


def test_delete_insert_refuses_a_null_key_and_rolls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, DELETE_INSERT)
    feed(tmp_path, rows((1, "n", "1.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(
        tmp_path,
        "(NULL, 'n', 1, 1, '2024-01-01 00:00:00+00'), (2, 'n', 1, 1, '2024-01-01 00:00:00+00')",
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert out.splitlines()[-1].startswith("UNIQUE_KEY_NULL ")
    assert [row[0] for row in table(tmp_path)] == [1]


def test_partition_overwrite_replaces_only_loaded_partitions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, PARTITION, BY_MONTH)
    first = (
        "(1, 'n', '1.0000', 1, '2024-01-10 10:00:00+00'), "
        "(2, 'n', '2.0000', 1, '2024-02-10 10:00:00+00'), "
        "(3, 's', '3.0000', 1, '2024-02-11 10:00:00+00'), "
        "(4, 's', '4.0000', 1, NULL)"
    )
    feed(tmp_path, first)
    assert command(capsys, "run", str(tmp_path))[0] == 0
    # The new rows of 2024-02 include one older than every row of it in the table, so the
    # partition is replaced (a run that only had newer rows of it would fail with
    # PARTITION_INCOMPLETE, #155).
    feed(
        tmp_path,
        "(5, 'n', '5.0000', 1, '2024-02-05 10:00:00+00'), (6, 'n', '6.0000', 1, NULL)",
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert lake_value(
        tmp_path, "SELECT month, id, CAST(kwh AS VARCHAR) FROM lake.s.m ORDER BY month, id"
    ) == [("2024-01", 1, "1.0000"), ("2024-02", 5, "5.0000"), (None, 6, "6.0000")]


def test_partition_overwrite_needs_partition_by(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, PARTITION.replace(", partition_by: [month]", ""), BY_MONTH)
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("MISSING_PARTITION_BY models/s/m.yaml")
    assert not (tmp_path / f".{SLUG}").exists()


def columns(folder: Path) -> list[tuple[object, ...]]:
    return lake_value(
        folder,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_catalog = 'lake' AND table_schema = 's' AND table_name = 'm' "
        "ORDER BY ordinal_position",
    )


NARROW = "SELECT id, read_at FROM feed.data"
WITH_KWH = "SELECT id, kwh, read_at FROM feed.data"


def test_append_new_columns_adds_a_column_and_refuses_removing_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND + "on_schema_change: append_new_columns\n", NARROW)
    feed(tmp_path, rows((1, "n", "1.0001")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    write(tmp_path, {"models/s/m.sql": WITH_KWH})
    feed(tmp_path, rows((2, "n", "2.0002")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert columns(tmp_path) == [
        ("id", "BIGINT"),
        ("read_at", "TIMESTAMP WITH TIME ZONE"),
        ("kwh", "DECIMAL(18,4)"),
    ]
    assert lake_value(tmp_path, "SELECT id, CAST(kwh AS VARCHAR) FROM lake.s.m ORDER BY id") == [
        (1, None),
        (2, "2.0002"),
    ]
    # Back to fewer columns: removing is not allowed under append_new_columns.
    write(tmp_path, {"models/s/m.sql": NARROW})
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "SCHEMA_CHANGED" in out
    assert len(columns(tmp_path)) == 3
    assert lake_value(tmp_path, "SELECT count(*) FROM lake.s.m") == [(2,)]


def test_sync_all_columns_drops_widens_and_refuses_lossy_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    narrow_decimal = "SELECT id, CAST(kwh AS DECIMAL(10,2)) AS kwh, region, read_at FROM feed.data"
    project(tmp_path, APPEND + "on_schema_change: sync_all_columns\n", narrow_decimal)
    feed(tmp_path, rows((1, "n", "12345678.99"), (2, "n", "-0.01")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    # region removed, kwh widened to DECIMAL(18,2) (the source is DECIMAL(18,4), so the new
    # value has 14 integer digits, more than DECIMAL(10,2) holds).
    write(
        tmp_path,
        {"models/s/m.sql": "SELECT id, CAST(kwh AS DECIMAL(18,2)) AS kwh, read_at FROM feed.data"},
    )
    feed(tmp_path, rows((3, "n", "99999999999999.99")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert columns(tmp_path) == [
        ("id", "BIGINT"),
        ("kwh", "DECIMAL(18,2)"),
        ("read_at", "TIMESTAMP WITH TIME ZONE"),
    ]
    assert lake_value(tmp_path, "SELECT id, CAST(kwh AS VARCHAR) FROM lake.s.m ORDER BY id") == [
        (1, "12345678.99"),
        (2, "-0.01"),
        (3, "99999999999999.99"),
    ]
    # A VARCHAR column that would become INTEGER: refused, nothing written.
    write(
        tmp_path,
        {
            "models/s/m.sql": (
                "SELECT id, CAST(kwh AS DECIMAL(18,2)) AS kwh, region AS code, read_at "
                "FROM feed.data"
            )
        },
    )
    feed(tmp_path, rows((4, "n", "4.00")))  # a later row: append reads only those (#135)
    assert command(capsys, "run", str(tmp_path))[0] == 0  # adds `code` VARCHAR
    write(
        tmp_path,
        {
            "models/s/m.sql": (
                "SELECT id, CAST(kwh AS DECIMAL(18,2)) AS kwh, CAST(id AS INTEGER) AS code, "
                "read_at FROM feed.data"
            )
        },
    )
    before = lake_value(tmp_path, "SELECT count(*) FROM lake.s.m")
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "SCHEMA_CHANGED" in out
    assert columns(tmp_path)[-1] == ("code", "VARCHAR")
    assert lake_value(tmp_path, "SELECT count(*) FROM lake.s.m") == before


ALL_AS_TEXT = "SELECT CAST(COLUMNS(*) AS VARCHAR) FROM lake.s.m ORDER BY ALL"


@pytest.mark.parametrize("policy", ["append_new_columns", "sync_all_columns"])
def test_a_later_failure_rolls_back_a_schema_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], policy: str
) -> None:
    project(tmp_path, APPEND + f"on_schema_change: {policy}\n", NARROW)
    feed(tmp_path, rows((1, "n", "1.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    before = (columns(tmp_path), lake_value(tmp_path, ALL_AS_TEXT))
    write(
        tmp_path,
        {
            "models/s/m.sql": WITH_KWH,
            "models/s/later.sql": (
                "SELECT CAST(CAST(kwh AS VARCHAR) || 'x' AS INTEGER) AS n FROM s.m"
            ),
        },
    )
    feed(tmp_path, rows((2, "n", "2.0000")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "s.later" in out
    assert (
        columns(tmp_path),
        lake_value(tmp_path, ALL_AS_TEXT),
    ) == before


@pytest.mark.parametrize(
    ("before", "after", "value"),
    [
        ("INTEGER", "BIGINT", "id"),
        ("TINYINT", "HUGEINT", "id"),
        ("UINTEGER", "UHUGEINT", "id"),
        ("DECIMAL(18,4)", "DECIMAL(38,4)", "kwh"),  # crosses DuckDB's 64-bit decimal storage
        ("DATE", "TIMESTAMP", "read_at"),
    ],
)
def test_every_allowed_widening_applies_in_the_lake_with_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], before: str, after: str, value: str
) -> None:
    """Each type change `sync_all_columns` allows is accepted by the lake and keeps old values."""

    def model_sql(kind: str) -> str:
        return f"SELECT id, CAST({value} AS {kind}) AS v, read_at FROM feed.data"

    project(tmp_path, MERGE + "on_schema_change: sync_all_columns\n", model_sql(before))
    feed(tmp_path, rows((1, "n", "1.2345"), (2, "n", "2.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    old = lake_value(tmp_path, "SELECT id, CAST(v AS VARCHAR) FROM lake.s.m WHERE id = 1")
    write(tmp_path, {"models/s/m.sql": model_sql(after)})
    feed(tmp_path, rows((2, "s", "7.0000"), (3, "s", "3.0000")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert columns(tmp_path)[1] == ("v", after)
    # Key 1 was not loaded again: its value is the old one, unchanged by the widening.
    new = lake_value(tmp_path, "SELECT id, CAST(v AS VARCHAR) FROM lake.s.m WHERE id = 1")
    if before == "DATE":
        assert new == [(1, str(old[0][1]) + " 00:00:00")]
    else:
        assert new == old
    assert [row[0] for row in lake_value(tmp_path, "SELECT id FROM lake.s.m ORDER BY id")] == [
        1,
        2,
        3,
    ]
