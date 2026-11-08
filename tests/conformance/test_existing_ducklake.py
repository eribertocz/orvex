"""The existing-DuckLake engine, end to end (P2-30 PR 3, #399; ADR-0027 §3, §5, §7).

A real existing lake (a SQLite catalog, its data on a file system of its own) holds the
datasets' tables, written by its owner; the managed lake holds the same tables and the policies'
mapping tables. Questions run through the semantic engine with the engine's adapter, its guarded
reads and its snapshot schedule, and are compared with the same question on the managed lake.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import jsonschema
import pytest
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    ProtectedLocation,
    Snapshot,
    SqliteLakeLocation,
)
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.engines import EngineEntry, Routing
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.ossie.reader import read
from orvex.semantic.receipt.external import expired
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.server.engine_catalog import EXISTING_DUCKLAKE_CAPABILITIES
from orvex.server.existing_lake_adapter import ExistingLakeAdapter
from orvex.server.existing_lake_evidence import violated
from orvex.server.existing_lake_snapshots import SnapshotSchedule
from orvex.store.errors import StoreError
from orvex.store.existing_lake import ExistingLake
from orvex.store.existing_lake_reads import LakeReads

from conformance.runners import corpus_execution as ce
from tests.conformance import test_row_policies as rp
from tests.conformance.test_direct_query import CASES, ZONE, routed
from tests.conformance.test_execution import ask
from tests.support.policies import VENDORS, extension

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = json.loads((ROOT / "contracts/receipt/v1/schema.json").read_text("utf-8"))
PROJECT = "warehouse-project"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)
ENTRY = EngineEntry(
    "warehouse",
    "ducklake",
    "existing_ducklake",
    "sqlite:/owner/catalog.sqlite|/owner/data/",
    EXISTING_DUCKLAKE_CAPABILITIES,
)


def _types(table: str) -> str:
    return ", ".join(f'"{c}" {kind.type}' for c, kind in rp.CATALOG[table].items())


def _value(text: str) -> str:
    return "NULL" if text == "" else f"'{text}'"


@dataclass
class Owner:
    """The other system that owns and writes the lake."""

    catalog: Path
    data: Path

    def write(self, *statements: str) -> None:
        writer = duckdb.connect()
        for name in ("ducklake", "sqlite"):
            writer.install_extension(name)
            writer.load_extension(name)
        writer.execute(
            f"ATTACH 'ducklake:sqlite:{self.catalog}' AS l "
            f"(DATA_PATH '{self.data.as_posix()}/', DATA_INLINING_ROW_LIMIT 0)"
        )
        for statement in statements:
            writer.execute(statement)
        writer.close()

    def edit_catalog(self, sql: str, *parameters: object) -> None:
        connection = sqlite3.connect(self.catalog)
        with connection:
            connection.execute(sql, parameters)
        connection.close()


@dataclass
class Engine:
    owner: Owner
    lake: ExistingLake
    reads: LakeReads
    schedule: SnapshotSchedule
    adapter: ExistingLakeAdapter
    attachment: ExistingLakeAttachment
    holds: list[str] = field(default_factory=list)  # evidence the next check reports violated


@pytest.fixture
def owner() -> Iterator[Owner]:
    shared = Path("/dev/shm")
    if not shared.is_dir() or shared.stat().st_dev == Path(tempfile.gettempdir()).stat().st_dev:
        if os.environ.get("CI"):
            pytest.fail("CI needs /dev/shm on a file system of its own")
        pytest.skip("no second file system here")
    root = Path(tempfile.mkdtemp(dir=shared, prefix="existing-ducklake-"))
    made = Owner(root / "catalog.sqlite", root / "data")
    made.data.mkdir()
    made.write(
        *(f"CREATE TABLE l.{table} ({_types(table)})" for table in rp.CATALOG),
        *(
            f"INSERT INTO l.{table} VALUES ({', '.join(map(_value, row))})"
            for table, rows in rp.DATA.items()
            for row in rows
        ),
    )
    connection = sqlite3.connect(made.catalog)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.close()
    yield made
    shutil.rmtree(root, ignore_errors=True)


def make_attachment(owner: Owner, protected: list[str]) -> ExistingLakeAttachment:
    return ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name="warehouse",
        location=SqliteLakeLocation(
            kind="lake_location",
            catalog="sqlite",
            path=str(owner.catalog),
            data_path=owner.data.as_posix() + "/",
        ),
        roles_in_use=[],
        protected_files=[],
        protected_locations=[ProtectedLocation(path) for path in protected],
    )


@pytest.fixture
def engine(owner: Owner, tmp_path: Path) -> Iterator[Engine]:
    made = make_attachment(owner, [str(tmp_path / "project")])
    lake = ExistingLake(made, memory_limit="512MB", threads=1)
    reads = LakeReads(lake, timeout_ms=60_000)
    holds: list[str] = []
    current = {"attachment": made}
    schedule = SnapshotSchedule(
        PROJECT,
        "warehouse",
        reads,
        attachment=lambda: current["attachment"],
        evidence=lambda snapshot: holds.pop() if holds else None,
        schedule="5m",
        freshness="1h",
    )
    built = Engine(owner, lake, reads, schedule, ExistingLakeAdapter(reads, schedule, "x"), made)
    built.holds = holds
    assert schedule.check(NOW).color == "green"
    yield built
    lake.close()


def ask_existing(
    managed: Any,  # noqa: ANN401 - a managed `Lake`
    adapter: ExistingLakeAdapter | None,
    policy: Any,  # noqa: ANN401
    context: dict[str, Any],
    document: dict[str, Any] | None = None,
    routing: Routing | None = None,
) -> Answer:
    adapters = {adapter.connection: adapter} if adapter is not None else {}
    return answer(
        json.dumps(document or rp.query()),
        rp.COMPILED,
        policy,
        dict(context),
        ce.engine_for(
            managed,
            routing=routing or routed(("sales", ENTRY), ("customers", ENTRY)),
            adapters=adapters,
        ),
    )


# --- results identical to the managed lake ---------------------------------------------------


@pytest.mark.parametrize(
    ("label", "rules", "attributes", "changed"), CASES, ids=[c[0] for c in CASES]
)
def test_an_existing_lake_answers_exactly_as_the_managed_lake(
    engine: Engine,
    label: str,
    rules: list[dict[str, Any]],
    attributes: dict[str, Any],
    changed: dict[str, Any],
) -> None:
    policy = rp.policies(*rules)
    context = rp.caller(**attributes)
    with rp.lake_of(mappings=rp.MAPPINGS | changed) as managed:
        expected = ask(rp.query(), rp.COMPILED, managed, policy=policy, context=context)
        found = ask_existing(managed, engine.adapter, policy, context)
    assert ce.rows_of(found) == ce.rows_of(expected)
    assert found.result.envelope()["columns"] == expected.result.envelope()["columns"]


# --- the switch: no adapter, no route --------------------------------------------------------


def test_without_the_engine_a_dataset_on_the_lake_is_refused_before_any_read() -> None:
    with rp.lake_of() as managed, pytest.raises(SemanticError) as caught:
        ask_existing(managed, None, rp.policies(), rp.caller())
    assert caught.value.code == "UNSUPPORTED_OPERATION"
    assert dict(caught.value.details) == {"operation": "route"}


# --- one engine per question -----------------------------------------------------------------


def test_a_join_with_a_managed_dataset_is_cross_engine(engine: Engine) -> None:
    document = rp.query(("customers.country",))
    with rp.lake_of() as managed, pytest.raises(SemanticError) as caught:
        ask_existing(
            managed,
            engine.adapter,
            rp.policies(),
            rp.caller(),
            document,
            Routing({"sales": ENTRY, "customers": None}),
        )
    assert caught.value.code == "CROSS_ENGINE_QUERY"
    assert dict(caught.value.details)["datasets"] == ["customers", "sales"]


# --- receipts (ADR-0027 §5) ------------------------------------------------------------------


def builder_view(found: Answer, policy: Any) -> dict[str, Any]:  # noqa: ANN401
    context = rp.caller() | {"role": "builder"}
    view = found.receipt.view(CallerViewer(rp.COMPILED, policy, context))
    jsonschema.validate(view, RECEIPT)
    return view


def test_the_receipt_names_the_external_snapshot_and_the_policies_managed_snapshot(
    engine: Engine,
) -> None:
    policy = rp.policies(ZONE)
    served = engine.schedule.state(NOW).served
    assert served is not None
    with rp.lake_of() as managed:
        found = ask_existing(managed, engine.adapter, policy, rp.caller(user_id="u-1"))
        managed_snapshot = managed.current_snapshot().id
    view = builder_view(found, policy)
    reads = {(r["for"], r["name"]): r for r in view["data"]}
    sales = reads[("dataset", "sales")]
    assert sales["route"] == "existing_ducklake"
    assert sales["snapshot"]["id"] == served.id
    assert dt.datetime.fromisoformat(sales["snapshot"]["committed_at"]) == served.committed_at
    assert (sales["snapshot"]["project"], sales["snapshot"]["connection"]) == (
        PROJECT,
        "warehouse",
    )
    zone = reads[("policy", "own_zone")]
    assert (zone["route"], zone["snapshot"]["id"]) == ("managed", managed_snapshot)
    assert view["reproducibility"]["class"] == "exact"


# --- a restored catalog (ADR-0027 §5) --------------------------------------------------------


def answered(engine: Engine) -> Answer:
    with rp.lake_of() as managed:
        return ask_existing(managed, engine.adapter, rp.policies(), rp.caller())


def test_a_restored_catalog_that_reuses_an_id_serves_nothing_and_old_receipts_explain(
    engine: Engine,
) -> None:
    before = answered(engine)
    assert before.receipt.reproducibility["class"] == "exact"
    served = engine.schedule.state(NOW).served
    assert served is not None
    # The catalog restored from a backup and written again: the same id, another commit.
    engine.owner.edit_catalog(
        "UPDATE ducklake_snapshot SET snapshot_time = '2026-11-08 11:00:00+00' "
        "WHERE snapshot_id = ?",
        served.id,
    )
    state = engine.schedule.check(NOW + dt.timedelta(minutes=5))
    # Treated as new: the snapshot it now lists passed, and is a snapshot of its own.
    assert state.served is not None
    assert state.served.id == served.id
    assert state.served.committed_at != served.committed_at
    assert answered(engine).receipt.reproducibility["class"] == "exact"
    later = expired(before.receipt, engine.schedule.kept, ce.SEAL_KEY)
    assert later.reproducibility == {
        "class": "explainable",
        "reasons": ["snapshot_expired"],
        "replay": "not_checked",
    }
    assert [r["retained"] for r in later.document()["data"]] == [False]


def test_a_catalog_whose_latest_id_went_back_serves_nothing_until_a_snapshot_passes(
    engine: Engine,
) -> None:
    before = answered(engine)
    engine.owner.write("INSERT INTO l.customers VALUES (3, 'CL')")
    assert engine.schedule.check(NOW + dt.timedelta(minutes=5)).color == "green"
    newer = engine.schedule.state(NOW).served
    assert newer is not None
    after_newer = answered(engine)
    # Restored to before the newer snapshot: its rows are gone from the catalog.
    engine.owner.edit_catalog("DELETE FROM ducklake_snapshot WHERE snapshot_id = ?", newer.id)
    engine.holds.append("unique of sales")  # and the snapshot it now offers does not pass
    state = engine.schedule.check(NOW + dt.timedelta(minutes=10))
    assert (state.color, state.served) == ("red", None)
    assert "held back" in str(state.reason)
    with pytest.raises(StoreError) as caught:
        answered(engine)
    assert caught.value.details["reason"] == "no_verified_snapshot"
    for receipt in (before.receipt, after_newer.receipt):
        assert expired(receipt, engine.schedule.kept, ce.SEAL_KEY).reproducibility["class"] == (
            "explainable"
        )
    # A later check with nothing violated serves again, as a new lake.
    assert engine.schedule.check(NOW + dt.timedelta(minutes=15)).color == "green"
    assert answered(engine).receipt.reproducibility["class"] == "exact"


def test_a_read_that_finds_its_pinned_snapshot_changed_makes_the_lake_new(
    engine: Engine,
) -> None:
    served = engine.schedule.state(NOW).served
    assert served is not None
    original = engine.adapter.pin()
    engine.owner.edit_catalog(
        "UPDATE ducklake_snapshot SET snapshot_time = '2026-11-08 11:00:00+00' "
        "WHERE snapshot_id = ?",
        served.id,
    )
    with pytest.raises(StoreError) as caught:
        engine.adapter.read("SELECT 1 AS one", [], original, 16, None)
    assert caught.value.details["reason"] == "snapshot_changed"
    state = engine.schedule.state(NOW)
    assert (state.color, state.served) == ("red", None)
    assert engine.schedule.due(NOW)


# --- evidence and files on each new snapshot (ADR-0027 §7) ----------------------------------


def evidenced() -> CompiledModel:
    """The row-policy model with `unique` evidence declared on `sales`."""
    document: dict[str, Any] = {
        "version": "0.2.0.dev0",
        "name": "tenants",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": [next(iter(columns))],
                "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
            }
            for t, columns in rp.CATALOG.items()
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    document["datasets"][0]["custom_extensions"] = [
        extension({"element": "dataset", "evidence": ["unique"]})
    ]
    loaded = read(json.dumps(document).encode(), "semantic/tenants.ossie.json", VENDORS)
    return compile_model(loaded, dict(rp.CATALOG), VENDORS)


def test_a_snapshot_with_violated_evidence_is_held_back_and_the_previous_one_served(
    owner: Owner, tmp_path: Path
) -> None:
    made = make_attachment(owner, [str(tmp_path / "project")])
    routing = routed(("sales", ENTRY), ("customers", ENTRY))
    compiled = evidenced()
    with ExistingLake(made, memory_limit="512MB", threads=1) as lake:
        reads = LakeReads(lake, timeout_ms=60_000)

        def evidence(snapshot: Snapshot) -> str | None:
            return violated(
                compiled,
                routing,
                "warehouse",
                lambda request: reads.read(request, snapshot.committed_at),
                snapshot,
                NOW,
            )

        schedule = SnapshotSchedule(
            PROJECT,
            "warehouse",
            reads,
            attachment=lambda: made,
            evidence=evidence,
            schedule="5m",
            freshness=None,
        )
        adapter = ExistingLakeAdapter(reads, schedule, "x")
        assert schedule.check(NOW).color == "green"
        previous = schedule.state(NOW).served
        assert previous is not None
        owner.write("INSERT INTO l.sales VALUES (1, 'A', 1, 1, 'North', 1.00)")  # sale 1 twice
        state = schedule.check(NOW + dt.timedelta(minutes=5))
        assert (state.color, state.served) == ("red", previous)
        assert str(state.reason).endswith("held back: evidence violated: unique of sales")
        with rp.lake_of() as managed:
            expected = ce.rows_of(ask(rp.query(), rp.COMPILED, managed, policy=rp.policies()))
            found = ask_existing(managed, adapter, rp.policies(), rp.caller())
        assert ce.rows_of(found) == expected  # the previous snapshot's rows, not the duplicate's
        owner.write("DELETE FROM l.sales WHERE amount = 1.00")
        assert schedule.check(NOW + dt.timedelta(minutes=10)).color == "green"
        assert adapter.pin()["id"] != previous.id


def test_a_snapshot_whose_files_fail_is_held_back(engine: Engine, tmp_path: Path) -> None:
    previous = engine.schedule.state(NOW).served
    outside = tmp_path / "outside.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1::BIGINT AS sale_id) TO '{outside}'")
    engine.owner.write("INSERT INTO l.customers VALUES (9, 'AR')")
    engine.owner.edit_catalog(
        "UPDATE ducklake_data_file SET path = ?, path_is_relative = 0 "
        "WHERE data_file_id = (SELECT max(data_file_id) FROM ducklake_data_file)",
        str(outside),
    )
    state = engine.schedule.check(NOW + dt.timedelta(minutes=5))
    assert (state.color, state.served) == ("red", previous)
    assert "held back" in str(state.reason)


def test_a_protected_location_registered_later_stops_serving(owner: Owner, tmp_path: Path) -> None:
    made = make_attachment(owner, [str(tmp_path / "project")])
    current = {"attachment": made}
    with ExistingLake(made, memory_limit="512MB", threads=1) as lake:
        reads = LakeReads(lake, timeout_ms=60_000)
        schedule = SnapshotSchedule(
            PROJECT,
            "warehouse",
            reads,
            attachment=lambda: current["attachment"],
            evidence=lambda _: None,
            schedule="5m",
            freshness=None,
        )
        assert schedule.check(NOW).color == "green"
        current["attachment"] = make_attachment(
            owner, [str(tmp_path / "project"), str(owner.data / "another-project")]
        )
        state = schedule.check(NOW + dt.timedelta(minutes=5))
        assert (state.color, state.served) == ("red", None)
        with pytest.raises(StoreError):
            schedule.pin()


def test_the_state_turns_amber_past_the_declared_freshness(engine: Engine) -> None:
    served = engine.schedule.state(NOW).served
    assert served is not None
    assert engine.schedule.state(served.committed_at + dt.timedelta(minutes=59)).color == "green"
    late = engine.schedule.state(served.committed_at + dt.timedelta(hours=2))
    assert (late.color, late.served) == ("amber", served)
