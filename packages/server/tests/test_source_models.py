"""#372: models that read a database source, end to end. Each one reads that connection alone
and is landed by the source's worker; validation, incremental loads with `$since`, tests and
reconciliation all keep working, and the source's own views are read."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import duckdb
from orvex.contracts.store.v1 import ReadRequest
from orvex.server import commands
from orvex.server.identity import load_identity
from orvex.server.project import load_project

IDENTITY = load_identity()
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def sources(folder: Path) -> None:
    data = folder / "data"
    data.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(data / "billing.duckdb")) as billing:
        billing.sql(
            "CREATE TABLE bills AS SELECT * FROM (VALUES "
            "(1, 'C1', 12.3456::DECIMAL(18,4)), (2, 'C1', 0.0001::DECIMAL(18,4)), "
            "(3, 'C2', -1.0000::DECIMAL(18,4))) AS t(id, customer, amount)"
        )
        billing.sql("CREATE MACRO positive(x) AS x > 0")
        billing.sql("CREATE VIEW billed AS SELECT * FROM bills WHERE positive(amount)")
    connection = sqlite3.connect(data / "events.sqlite")
    connection.executescript(
        """
        CREATE TABLE events (id INTEGER, happened TEXT, kind TEXT);
        INSERT INTO events VALUES (1, '2026-01-01 10:00:00', 'open');
        INSERT INTO events VALUES (2, '2026-01-02 10:00:00', 'close');
        CREATE VIEW recent AS SELECT id, CAST(happened AS TIMESTAMP) AS happened, kind FROM events;
        """
    )
    connection.commit()
    connection.close()


CONNECTIONS = """\
version: 1
connections:
  billing:
    type: duckdb
    path: data/billing.duckdb
    read_only: true
  journal:
    type: sqlite
    path: data/events.sqlite
    read_only: true
"""

MODELS = {
    "models/staging/billed.sql": "SELECT id, customer, amount FROM billing.billed",
    "models/staging/billed.yaml": (
        "version: 1\ntests:\n  - not_null: [amount]\n"
        "reconciliation:\n  - name: total\n    measure: sum(amount)\n"
        "    group_by: [customer]\n    source: billing.billed\n    tolerance: 0\n"
    ),
    "models/staging/events.sql": "SELECT id, happened, kind FROM journal.recent",
    "models/staging/events.yaml": (
        "version: 1\nmaterialized: incremental\nincremental:\n  strategy: append\n"
        "  time_column: happened\n"
    ),
    "models/marts/by_customer.sql": (
        "SELECT customer, sum(amount) AS total FROM staging.billed GROUP BY customer"
    ),
}


def project(folder: Path, **extra: str) -> Path:
    sources(folder)
    write(
        folder,
        {"project.yaml": PROJECT, "connections.yaml": CONNECTIONS, **MODELS, **extra},
    )
    return folder


def run(folder: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = commands.run(folder, IDENTITY, out)
    return code, out.getvalue()


def read(folder: Path, sql: str) -> list[dict[str, object]]:
    home = folder / f".{IDENTITY.slug}"
    lake = commands.open_existing(home, commands.lake_location(load_project(folder), home))
    try:
        request = ReadRequest(
            kind="read",
            sql=sql,
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=1024,
        )
        found: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
        return found
    finally:
        lake.close()


def test_models_land_from_database_sources_through_their_views(tmp_path: Path) -> None:
    project(tmp_path)
    code, out = run(tmp_path)
    assert code == 0, out
    assert read(
        tmp_path,
        "SELECT customer, CAST(total AS VARCHAR) AS total FROM marts.by_customer ORDER BY 1",
    ) == [{"customer": "C1", "total": "12.3457"}]
    assert read(tmp_path, "SELECT typeof(amount) AS t FROM staging.billed LIMIT 1") == [
        {"t": "DECIMAL(18,4)"}
    ]
    assert read(tmp_path, "SELECT count(*) AS n FROM staging.events") == [{"n": 2}]


def test_an_incremental_source_model_loads_only_new_rows(tmp_path: Path) -> None:
    project(tmp_path)
    assert run(tmp_path)[0] == 0
    connection = sqlite3.connect(tmp_path / "data" / "events.sqlite")
    connection.execute("INSERT INTO events VALUES (3, '2026-01-03 10:00:00', 'open')")
    connection.commit()
    connection.close()
    code, out = run(tmp_path)
    assert code == 0, out
    assert read(tmp_path, "SELECT id FROM staging.events ORDER BY id") == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]


DOUBLED = "SELECT id, customer, amount * 2 AS amount FROM billing.billed"


def test_reconciliation_against_a_database_source_detects_a_difference(tmp_path: Path) -> None:
    """The source's groups land from its worker and are compared in the lake (#372)."""
    project(tmp_path, **{"models/staging/billed.sql": DOUBLED})
    code, out = run(tmp_path)
    assert code == 1
    assert "RECONCILIATION_FAILED models/staging/billed.yaml total failed on 1 groups" in out


def test_validation_refuses_a_mixed_source_model(tmp_path: Path) -> None:
    project(
        tmp_path,
        **{
            "models/marts/mixed.sql": (
                "SELECT b.id FROM billing.bills AS b JOIN staging.billed AS s ON s.id = b.id"
            )
        },
    )
    out = io.StringIO()
    assert commands.validate(tmp_path, out) == 1
    assert "SOURCE_MODEL_MIXED" in out.getvalue()
