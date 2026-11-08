"""P2-26b (#355): evidence checked after successful runs and on demand, kept in the lake,
used by questions on the snapshot they read, and `append_only` against the previous one."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
from orvex.semantic.evidence import Ledger
from orvex.server import commands, evidence_runs
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.server.semantic_model import load_semantic
from orvex.store.lake import Lake

IDENTITY = load_identity()
VENDORS = (IDENTITY.ossie_vendor_name, *IDENTITY.previous_vendor_names)
SLUG = IDENTITY.slug

PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"


def write(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def sources(folder: Path) -> Path:
    """Parquet readings (exact decimals) and CSV tariffs, outside the project's lake."""
    data = folder / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute(
        f"COPY (SELECT * FROM (VALUES (1, 'C1', 12.3456::DECIMAL(18,4)), "
        f"(2, 'C1', 0.0001::DECIMAL(18,4)), (3, 'C2', -1.0000::DECIMAL(18,4)), "
        f"(4, 'C2', 7.5000::DECIMAL(18,4))) AS t(reading_id, consumer_id, kwh)) "
        f"TO '{data / 'readings.parquet'}'"
    )
    connection.execute(
        f"COPY (SELECT * FROM (VALUES ('C1', 'residential'), ('C2', 'commercial')) "
        f"AS t(consumer_id, tariff)) TO '{data / 'consumers.csv'}'"
    )
    connection.close()
    return data


CONNECTIONS = """\
version: 1
connections:
  archive:
    type: parquet
    path: data/readings.parquet
  registry:
    type: csv
    path: data/consumers.csv
"""

MODELS = {
    "models/staging/readings.sql": "SELECT reading_id, consumer_id, kwh FROM archive.data",
    "models/staging/consumers.sql": "SELECT consumer_id, tariff FROM registry.data",
    "models/staging/active_consumers.sql": (
        "SELECT r.consumer_id, sum(r.kwh) AS kwh FROM staging.readings AS r "
        "WHERE r.kwh > 0 GROUP BY r.consumer_id"
    ),
    "models/staging/active_consumers.yaml": "version: 1\nmaterialized: view\n",
    "models/marts/consumption.sql": (
        "SELECT a.consumer_id, c.tariff, a.kwh FROM staging.active_consumers AS a "
        "JOIN staging.consumers AS c USING (consumer_id)"
    ),
}


def full_project(folder: Path) -> Path:
    sources(folder)
    return write(folder, {"project.yaml": PROJECT, "connections.yaml": CONNECTIONS, **MODELS})


def extension(payload: dict[str, Any]) -> dict[str, Any]:
    return {"vendor_name": VENDORS[0], "data": json.dumps({"version": 1, **payload})}


def model(evidence: tuple[str, ...] = ("unique",)) -> str:
    return json.dumps(
        {
            "version": "0.2.0.dev0",
            "name": "consumption",
            "datasets": [
                {
                    "name": "consumption",
                    "source": "marts.consumption",
                    "primary_key": ["consumer_id"],
                    "fields": [
                        {"name": c, "expression": {"sql": c}}
                        for c in ("consumer_id", "tariff", "kwh")
                    ],
                    "custom_extensions": [
                        extension({"element": "dataset", "evidence": list(evidence)})
                    ],
                }
            ],
            "metrics": [{"name": "total_kwh", "expression": {"sql": "SUM(consumption.kwh)"}}],
            "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
        }
    )


class opened:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def __enter__(self) -> Lake:
        home = self.folder / f".{SLUG}"
        place = commands.lake_location(load_project(self.folder), home)
        self.lake = commands.open_existing(home, place)
        return self.lake

    def __exit__(self, *_: object) -> None:
        self.lake.close()


def run(folder: Path, **more: Any) -> tuple[int, str]:  # noqa: ANN401
    out = io.StringIO()
    return commands.run(folder, IDENTITY, out, **more), out.getvalue()


def project(folder: Path, evidence: tuple[str, ...] = ("unique",)) -> Path:
    full_project(folder)
    return write(folder, {"semantic/consumption.ossie.json": model(evidence)})


def stored(folder: Path) -> list[Any]:
    with opened(folder) as lake:
        return evidence_runs.records_at(lake, lake.current_snapshot().id)


def test_a_successful_run_checks_and_records_the_evidence_of_what_it_wrote(
    tmp_path: Path,
) -> None:
    project(tmp_path)
    code, out = run(tmp_path)
    assert code == 0, out
    (record,) = stored(tmp_path)
    assert (record.name, record.kind, record.status) == ("CONSUMPTION", "unique", "verified")
    assert record.tables == ("marts.consumption",)
    assert record.run is not None
    assert f"evidence at snapshot {record.snapshot}: 1 verified, 0 violated, 0 declared" in out


def test_a_failed_run_checks_nothing(tmp_path: Path) -> None:
    project(tmp_path)
    write(tmp_path, {"models/marts/consumption.sql": "SELECT nope FROM staging.consumers"})
    code, out = run(tmp_path)
    assert code == 1
    assert "evidence" not in out
    with opened(tmp_path) as lake:
        assert evidence_runs.records_at(lake, lake.current_snapshot().id) == []


def test_a_violation_is_recorded_and_reported(tmp_path: Path) -> None:
    project(tmp_path)
    write(  # every consumer twice: the declared key repeats
        tmp_path,
        {
            "models/marts/consumption.sql": (
                "SELECT a.consumer_id, c.tariff, a.kwh FROM staging.active_consumers AS a "
                "JOIN staging.consumers AS c USING (consumer_id) "
                "UNION ALL SELECT a.consumer_id, 'x', a.kwh FROM staging.active_consumers AS a"
            )
        },
    )
    code, out = run(tmp_path)
    assert code == 0, out  # the run went as it went: evidence is reported, never undone
    assert "EVIDENCE_VIOLATED consumption unique duplicates 2" in out
    (record,) = stored(tmp_path)
    assert (record.status, dict(record.violations)) == ("violated", {"duplicates": 2})


def test_records_survive_a_restart_and_hold_on_the_snapshot_that_wrote_them(
    tmp_path: Path,
) -> None:
    """Writing the records commits a snapshot of its own; the record of the run's snapshot
    holds there, since no table its check read changed, and after a reopening."""
    project(tmp_path)
    run(tmp_path)
    with opened(tmp_path) as lake:
        latest = lake.current_snapshot().id
        records = evidence_runs.records_at(lake, latest)
        (record,) = records
        assert record.snapshot < latest
        compiled = load_semantic(tmp_path, lake, latest, VENDORS, audience="builder").model
        assert compiled is not None
        ledger = Ledger(compiled.compiled, records, latest, evidence_runs.unchanged(lake))
        assert ledger.verified(record.key)
        assert not Ledger(compiled.compiled, records, latest).verified(record.key)  # no proof
    assert stored(tmp_path) == records  # the lake reopened: the same records


def test_a_later_run_that_changes_the_table_stops_the_old_record_from_holding(
    tmp_path: Path,
) -> None:
    project(tmp_path)
    run(tmp_path)
    (first,) = stored(tmp_path)
    write(tmp_path, {"semantic/consumption.ossie.json": model(("non_overlap",))})
    run(tmp_path)  # the table is rebuilt; its `unique` is no longer declared, so not checked
    with opened(tmp_path) as lake:
        latest = lake.current_snapshot().id
        assert not evidence_runs.unchanged(lake)(first, latest)
        loaded = load_semantic(tmp_path, lake, latest, VENDORS, audience="builder").model
        assert loaded is not None
        assert Ledger(loaded.compiled, [first], latest, evidence_runs.unchanged(lake)).records == {}


FEED = """\
version: 1
connections:
  feed:
    type: parquet
    path: data/feed.parquet
"""


def feed(folder: Path, *rows: tuple[int, str, str]) -> None:
    """The source rows: id, kwh, and a read time on day id of January 2024."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    values = ", ".join(
        f"({i}, '{kwh}', '2024-01-{i:02d} 10:00:00+00', '{t}')" for i, kwh, t in rows
    )
    duckdb.connect().execute(
        "COPY (SELECT id::BIGINT AS id, kwh::DECIMAL(18,4) AS kwh, read_at::TIMESTAMPTZ AS "
        f"read_at FROM (VALUES {values}) AS t(id, kwh, read_at, tag)) "
        f"TO '{folder / 'data' / 'feed.parquet'}'"
    )


def appended(folder: Path, strategy: str) -> None:
    """A project whose one model `s.m` loads the feed incrementally, with `append_only`."""
    keys = ", unique_key: [id]" if strategy == "merge" else ""
    where = "read_at > $since" if strategy == "append" else "$since IS NOT NULL"
    document = json.loads(model(("append_only",)))
    document["datasets"][0] |= {
        "name": "readings",
        "source": "s.m",
        "primary_key": ["id"],
        "fields": [{"name": c, "expression": {"sql": c}} for c in ("id", "kwh", "read_at")],
    }
    document["metrics"] = [{"name": "kwh", "expression": {"sql": "SUM(readings.kwh)"}}]
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": FEED,
            "models/s/m.sql": (
                f"SELECT * FROM (SELECT id, kwh, read_at FROM feed.data) WHERE {where}"
            ),
            "models/s/m.yaml": (
                "version: 1\nmaterialized: incremental\nincremental: "
                f"{{strategy: {strategy}, time_column: read_at{keys}, filter: manual}}\n"
            ),
            "semantic/readings.ossie.json": json.dumps(document),
        },
    )


def test_append_only_is_declared_first_then_checked_against_the_previous_snapshot(
    tmp_path: Path,
) -> None:
    project(tmp_path, ("append_only",))
    code, out = run(tmp_path)
    assert code == 0, out
    assert "declared consumption append_only: no earlier snapshot to compare against" in out
    code, out = run(tmp_path)  # a table model rebuilt with the same rows: every one remains
    assert code == 0, out
    assert "1 verified, 0 violated, 0 declared" in out
    write(  # rebuilt without the commercial consumer: one row of the baseline is gone
        tmp_path,
        {
            "models/marts/consumption.sql": (
                "SELECT a.consumer_id, c.tariff, a.kwh FROM staging.active_consumers AS a "
                "JOIN staging.consumers AS c USING (consumer_id) WHERE c.tariff = 'residential'"
            )
        },
    )
    code, out = run(tmp_path)
    assert code == 0, out
    assert "EVIDENCE_VIOLATED consumption append_only removed 1" in out


def test_append_only_holds_for_a_model_that_only_appends(tmp_path: Path) -> None:
    appended(tmp_path, "append")
    feed(tmp_path, (1, "1.0000", ""), (2, "2.0000", ""))
    code, out = run(tmp_path)
    assert code == 0, out
    assert "declared readings append_only: no earlier snapshot to compare against" in out
    feed(tmp_path, (1, "1.0000", ""), (2, "2.0000", ""), (3, "3.0000", ""))
    code, out = run(tmp_path)  # one row added: every earlier row remains
    assert code == 0, out
    assert "1 verified, 0 violated, 0 declared" in out
    (record,) = stored(tmp_path)
    assert (record.kind, record.status, dict(record.violations)) == (
        "append_only",
        "verified",
        {"removed": 0},
    )


def test_append_only_counts_the_rows_a_merge_changed(tmp_path: Path) -> None:
    appended(tmp_path, "merge")
    feed(tmp_path, (1, "1.0000", ""), (2, "2.0000", ""), (3, "3.0000", ""))
    run(tmp_path)
    feed(tmp_path, (1, "1.5000", ""), (2, "2.5000", ""), (3, "3.0000", ""), (4, "4.0000", ""))
    code, out = run(tmp_path)  # rows 1 and 2 changed, 3 kept, 4 added
    assert code == 0, out
    assert "EVIDENCE_VIOLATED readings append_only removed 2" in out


def test_on_demand_a_builder_checks_one_dataset_or_all(tmp_path: Path) -> None:
    project(tmp_path)
    run(tmp_path)
    out = io.StringIO()
    assert commands.evidence(tmp_path, IDENTITY, out, "consumption") == 0
    assert "1 verified" in out.getvalue()
    out = io.StringIO()
    assert commands.evidence(tmp_path, IDENTITY, out, "nowhere") == 1
    assert "no dataset 'nowhere'" in out.getvalue()
    out = io.StringIO()
    assert commands.evidence(tmp_path, IDENTITY, out) == 0


def test_on_demand_without_a_lake_checks_nothing(tmp_path: Path) -> None:
    project(tmp_path)
    out = io.StringIO()
    assert commands.evidence(tmp_path, IDENTITY, out) == 1
    assert out.getvalue().strip() == "evidence not checked: the lake has no snapshot yet"
    assert not (tmp_path / f".{SLUG}").exists()


def test_the_http_app_never_reaches_the_cli_builder_grant() -> None:
    """The grant lives in `evidence_command.evidence` (re-exported by the `commands` façade); the
    HTTP app imports nothing that holds it."""
    found = subprocess.run(
        [sys.executable, "-c", "import sys, orvex.server.app; print(sorted(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for module in (
        "orvex.server.commands",
        "orvex.server.evidence_command",
        "orvex.server.evidence_runs",
        "orvex.server.cli",
    ):
        assert f"'{module}'" not in found


def test_questions_use_the_records_of_their_snapshot_and_reproducibility_is_unchanged(
    tmp_path: Path,
) -> None:
    import yaml
    from orvex.semantic.execution import answer
    from orvex.semantic.policies import read_policies
    from orvex.semantic.result.cursors import SealedForm

    from conformance.runners import corpus_execution as ce

    project(tmp_path)
    run(tmp_path)
    query = json.dumps(
        {
            "kind": "semantic_query",
            "version": 1,
            "mode": "detail",
            "fields": [{"member": "consumption.consumer_id"}],
            "order": [{"by": "consumption.consumer_id", "direction": "asc"}],
            "page": {"size": "10"},
        }
    )
    with opened(tmp_path) as lake:
        latest = lake.current_snapshot().id
        loaded = load_semantic(tmp_path, lake, latest, VENDORS, audience="builder").model
        assert loaded is not None
        policies = read_policies(
            yaml.safe_dump({"version": 1}), loaded.compiled, groups=frozenset(), managed_tables={}
        )
        evidence, unchanged = evidence_runs.engine_evidence(lake)
        receipts = {}
        for name, more in (
            ("records", {"evidence": evidence, "unchanged": unchanged}),
            ("none", {}),
        ):
            engine = ce.engine_for(lake, cursors=SealedForm(b"k" * 32), **more)
            found = answer(query, loaded.compiled, policies, ce.viewer_context(), engine)
            receipts[name] = found.receipt.document()
    assert [e["status"] for e in receipts["records"]["evidence"]] == ["verified"]
    assert [e["status"] for e in receipts["none"]["evidence"]] == ["declared"]
    assert receipts["records"]["reproducibility"] == receipts["none"]["reproducibility"]
