"""The existing DuckLakes of the served project in the `serve` process (P2-30 PR 3, #399;
ADR-0027 §3, §5, §7, §8): attached with the switch on, checked on their schedule, supplied to
the HTTP questions at their served snapshot, and on one shared memory budget."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest
from orvex.access.local import LocalRequest
from orvex.server import cli, existing_lakes, semantic_http
from orvex.server.commands import project_lock
from orvex.server.existing_lakes import ExistingLakes, memory_share
from orvex.server.identity import load_identity
from orvex.server.instance import ProjectRegistry, home_variable
from orvex.server.project import load_project
from orvex.store.errors import StoreError
from orvex.store.existing_lake import ExistingLake

from .test_lake_separation import other_device  # noqa: F401 - a fixture
from .test_semantic_model import extension, model, ran

IDENTITY = load_identity()
SWITCH = IDENTITY.slug.upper().replace("-", "_") + "_DEV_EXISTING_DUCKLAKE"
NOW = dt.datetime(2026, 11, 8, 12, tzinfo=dt.UTC)
QUERY = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "aggregate",
    "group_by": [{"member": "consumption.tariff"}],
    "metrics": [{"metric": "total_kwh"}],
}


def on_billing(evidence: bool = False) -> str:
    document = json.loads(model(source="readings"))
    payload: dict[str, Any] = {"element": "dataset", "connection": "billing"}
    if evidence:
        payload["evidence"] = ["unique"]
    document["datasets"][0]["custom_extensions"] = [extension(payload)]
    return json.dumps(document)


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


@dataclass
class Served:
    folder: Path
    instance: Path
    owner: Owner


def connection(owner: Owner) -> str:
    return (
        "  billing:\n    type: ducklake\n    catalog:\n      type: sqlite\n"
        f"      path: {owner.catalog}\n    data_path: {owner.data.as_posix()}/\n"
        "    read_only: true\n    schedule: 5m\n"
    )


@pytest.fixture
def served(tmp_path: Path, other_device: Path) -> Iterator[Served]:  # noqa: F811
    owner = Owner(other_device / "catalog.sqlite", other_device / "data")
    owner.data.mkdir()
    owner.write(
        "CREATE TABLE l.readings (consumer_id VARCHAR, tariff VARCHAR, kwh DECIMAL(18, 4))",
        "INSERT INTO l.readings VALUES ('c1', 'R1', 1.5), ('c2', 'R1', 2.25), ('c3', 'R2', 4)",
    )
    wal = sqlite3.connect(owner.catalog)
    assert wal.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    wal.close()
    folder = tmp_path / "p"
    ran(folder, **{"semantic/m.ossie.json": on_billing()})
    text = (folder / "connections.yaml").read_text()
    (folder / "connections.yaml").write_text(text + connection(owner))
    instance = tmp_path / "instance"
    ProjectRegistry(instance).register(folder)
    yield Served(folder, instance, owner)
    shutil.rmtree(other_device, ignore_errors=True)


def opened(served: Served, memory: str = "256MB") -> Any:  # noqa: ANN401 - a context manager
    return existing_lakes.opened(
        served.folder,
        served.instance,
        IDENTITY.slug,
        "p",
        load_project(served.folder).connections,
        memory_limit=memory,
        threads=1,
        timeout_ms=60_000,
        engine_version="x",
    )


def ask(source: semantic_http.ProjectSource) -> semantic_http.Asked:
    semantic = semantic_http.Semantic(
        source, semantic_http.local_context, semantic_http.session_settings()
    )
    return semantic_http.ask(semantic, json.dumps(QUERY).encode(), LocalRequest("p", True))


def rows(asked: semantic_http.Asked) -> list[list[Any]]:
    return [list(row) for row in asked.json_document()["result"]["rows"]]


# --- one memory budget -------------------------------------------------------------------------


def test_every_instance_gets_an_equal_share_of_the_memory_budget() -> None:
    assert memory_share("8GB", 4) == "2048MB"
    assert memory_share("3000MB", 3) == "1000MB"
    assert memory_share("1TB", 1) == "1048576MB"
    assert memory_share("2MB", 3) == "1MB"
    for wrong in (("8 apples", 2), ("8GB", 0), ("", 1)):
        with pytest.raises(ValueError, match="not a memory budget"):
            memory_share(*wrong)


def test_the_managed_lake_and_each_existing_lake_share_the_budget(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setattr(cli, "memory_limit", lambda: "900MB")
    monkeypatch.setattr(cli, "instance_directory", lambda identity: served.instance)
    given: list[str] = []
    real = ExistingLake

    def recorded(attachment: Any, *, memory_limit: str, threads: int) -> Any:  # noqa: ANN401
        given.append(memory_limit)
        return real(attachment, memory_limit=memory_limit, threads=threads)

    monkeypatch.setattr(existing_lakes, "ExistingLake", recorded)
    opened_with: list[str | None] = []
    monkeypatch.setattr(
        cli, "open_existing", lambda home, place, memory=None: opened_with.append(memory)
    )
    with cli.attached_lakes(served.folder, IDENTITY, "p") as lakes:
        assert lakes is not None
        assert len(lakes) == 1
        source = cli.served_project(served.folder, IDENTITY, lakes)
        source.open_lake()
    assert given == ["450MB"]  # two instances: the managed lake's and this lake's
    assert opened_with == ["450MB"]


# --- the switch ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_with_the_switch_off_no_lake_is_attached_and_the_dataset_is_unsupported(
    served: Served, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, value)
    monkeypatch.setattr(cli, "instance_directory", lambda identity: served.instance)
    with cli.attached_lakes(served.folder, IDENTITY, "p") as lakes:
        assert lakes is None
        source = cli.served_project(served.folder, IDENTITY, lakes)
        with source.session() as lake:
            loaded = source.model(lake, lake.current_snapshot().id)
    assert loaded.model is None
    assert [p.code for p in loaded.problems] == ["UNSUPPORTED_OPERATION"]


def test_a_project_without_an_existing_lake_attaches_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    folder = tmp_path / "p"
    ran(folder, **{"semantic/m.ossie.json": model()})
    with cli.attached_lakes(folder, IDENTITY, "p") as lakes:
        assert lakes is None


# --- questions over HTTP, at the served snapshot ---------------------------------------------


def test_a_question_reads_the_existing_lake_at_its_served_snapshot(served: Served) -> None:
    with opened(served) as lakes:
        source = cli.served_project(served.folder, IDENTITY, lakes)
        with lakes.checking(source, poll=3600):
            asked = ask(source)
            assert rows(asked) == [["R1", "3.7500"], ["R2", "4.0000"]]
            served_id = lakes.schedule("billing").state(NOW).served
            assert served_id is not None
            reads = {(r["for"], r["name"]): r for r in asked.receipt["data"]}
            read = reads[("dataset", "consumption")]
            assert read["route"] == "existing_ducklake"
            assert read["snapshot"]["id"] == served_id.id
            assert (read["snapshot"]["project"], read["snapshot"]["connection"]) == ("p", "billing")
            # The owner commits; questions keep the served snapshot until the next check.
            served.owner.write("INSERT INTO l.readings VALUES ('c4', 'R2', 1)")
            assert rows(ask(source)) == [["R1", "3.7500"], ["R2", "4.0000"]]
            lakes.check(source)  # not due yet: nothing changes
            assert rows(ask(source)) == [["R1", "3.7500"], ["R2", "4.0000"]]


def test_a_lake_with_nothing_served_refuses_its_datasets_questions(served: Served) -> None:
    with opened(served) as lakes:
        source = cli.served_project(served.folder, IDENTITY, lakes)
        found = lakes.pinned()["billing"]  # never checked: nothing is served
        assert isinstance(found, StoreError)
        assert found.details["reason"] == "no_verified_snapshot"
        with pytest.raises(semantic_http.Refused) as refused:
            ask(source)
    assert refused.value.code == "SEMANTIC_MODEL_INVALID"
    assert [d[0] for d in refused.value.details] == ["semantic/m.ossie.json"]
    assert "STORE_SOURCE_INVALID" in refused.value.details[0][1]


def test_the_model_and_its_reads_use_one_pinned_snapshot(served: Served) -> None:
    with opened(served) as lakes:
        source = cli.served_project(served.folder, IDENTITY, lakes)
        clock = {"now": NOW}
        lakes._now = lambda: clock["now"]
        lakes.check(source)
        given = lakes.pinned()["billing"]
        assert not isinstance(given, StoreError)
        assert given.adapter is not None
        pinned = given.adapter.pin()
        assert given.tables.snapshot == pinned["id"]
        served.owner.write("INSERT INTO l.readings VALUES ('c5', 'R3', 1)")
        clock["now"] = NOW + dt.timedelta(hours=1)
        lakes.check(source)  # a newer snapshot is served now
        assert lakes.schedule("billing").pin()["id"] != pinned["id"]
        assert given.adapter.pin() == pinned  # the question's adapter stays where it compiled


# --- the schedule in the serve process ----------------------------------------------------------


def test_the_schedule_runs_in_the_serve_process_and_serves_new_snapshots(served: Served) -> None:
    clock = {"now": NOW}
    with opened(served) as lakes:
        lakes._now = lambda: clock["now"]
        source = cli.served_project(served.folder, IDENTITY, lakes)
        with lakes.checking(source, poll=0.05):
            first = lakes.schedule("billing").state(NOW).served
            assert first is not None  # checked before the first question
            served.owner.write("INSERT INTO l.readings VALUES ('c4', 'R2', 1)")
            clock["now"] = NOW + dt.timedelta(minutes=5)
            deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
            while lakes.schedule("billing").state(clock["now"]).served == first:
                assert dt.datetime.now(dt.UTC) < deadline, "the thread never checked the lake"
                time.sleep(0.05)
            assert rows(ask(source)) == [["R1", "3.7500"], ["R2", "5.0000"]]


def test_a_new_snapshot_with_violated_evidence_is_held_back_by_the_serve_process(
    served: Served,
) -> None:
    (served.folder / "semantic/m.ossie.json").write_text(on_billing(evidence=True))
    clock = {"now": NOW}
    with opened(served) as lakes:
        lakes._now = lambda: clock["now"]
        source = cli.served_project(served.folder, IDENTITY, lakes)
        lakes.check(source)
        schedule = lakes.schedule("billing")
        first = schedule.state(NOW).served
        assert first is not None
        served.owner.write("INSERT INTO l.readings VALUES ('c1', 'R9', 100)")  # c1 twice
        clock["now"] = NOW + dt.timedelta(minutes=5)
        lakes.check(source)
        state = schedule.state(clock["now"])
        assert (state.color, state.served) == ("red", first)
        assert str(state.reason).endswith("evidence violated: unique of consumption")
        assert rows(ask(source)) == [["R1", "3.7500"], ["R2", "4.0000"]]


def test_a_check_that_cannot_have_the_project_holds_a_new_snapshot_back(served: Served) -> None:
    clock = {"now": NOW}
    with opened(served) as lakes:
        lakes._now = lambda: clock["now"]
        source = cli.served_project(served.folder, IDENTITY, lakes)
        lakes.check(source)
        schedule = lakes.schedule("billing")
        first = schedule.state(NOW).served
        served.owner.write("INSERT INTO l.readings VALUES ('c6', 'R2', 1)")
        clock["now"] = NOW + dt.timedelta(minutes=5)
        with project_lock(source.home):  # a run holds the project's lake
            lakes.check(source)
        state = schedule.state(clock["now"])
        assert (state.color, state.served) == ("red", first)
        assert str(state.reason).endswith("cannot be checked: a run holds the project's lake")
        lakes.check(source)  # not due again until its period ends
        clock["now"] = NOW + dt.timedelta(minutes=10)
        lakes.check(source)
        assert schedule.state(clock["now"]).color == "green"


def test_a_lake_that_cannot_be_attached_leaves_none_open(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []
    real = ExistingLake.close

    def close(self: ExistingLake) -> None:
        closed.append(self.name)
        real(self)

    monkeypatch.setattr(ExistingLake, "close", close)
    text = (served.folder / "connections.yaml").read_text()
    broken = Path(tempfile.mkdtemp(dir="/dev/shm")) / "missing.sqlite"
    second = connection(Owner(broken, served.owner.data)).replace("  billing:", "  zz_broken:")
    (served.folder / "connections.yaml").write_text(text + second)
    with pytest.raises(StoreError), opened(served):
        pass
    assert closed == ["billing"]
    shutil.rmtree(broken.parent, ignore_errors=True)


def test_the_lakes_are_closed_when_the_serve_session_ends(served: Served) -> None:
    with opened(served) as lakes:
        assert isinstance(lakes, ExistingLakes)
        reads = lakes._lakes["billing"].reads
    with pytest.raises(Exception):  # noqa: B017, PT011 - the instance is closed
        reads.latest()


# --- `serve` itself -----------------------------------------------------------------------------


def test_serve_attaches_and_checks_the_lake_before_it_answers_and_closes_it_after(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    monkeypatch.setenv(home_variable(IDENTITY), str(served.instance))
    monkeypatch.setenv(SWITCH, "1")
    seen: dict[str, Any] = {}
    real = existing_lakes.opened

    @contextmanager
    def recorded(*arguments: Any, **settings: Any) -> Iterator[ExistingLakes]:  # noqa: ANN401
        with real(*arguments, **settings) as lakes:
            seen["lakes"] = lakes
            yield lakes

    def run(server: uvicorn.Server) -> None:  # while serving
        lakes = seen["lakes"]
        seen["state"] = lakes.schedule("billing").state(dt.datetime.now(dt.UTC))
        seen["reads"] = lakes._lakes["billing"].reads

    monkeypatch.setattr(existing_lakes, "opened", recorded)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    assert cli.main([str(served.folder), "--port", str(cli.free_port(17_100)), "--no-browser"]) == 0
    assert seen["state"].served is not None
    assert seen["state"].color == "green"
    with pytest.raises(Exception):  # noqa: B017, PT011 - closed with the session
        seen["reads"].latest()


def test_serve_does_not_start_when_a_lake_cannot_be_attached(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(home_variable(IDENTITY), str(served.instance))
    monkeypatch.setenv(SWITCH, "1")
    served.owner.catalog.unlink()
    port = cli.free_port(17_200)
    assert cli.main([str(served.folder), "--port", str(port), "--no-browser"]) == 1
    assert capsys.readouterr().err.startswith("STORE_")
