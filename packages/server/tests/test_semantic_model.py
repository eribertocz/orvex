"""P2-26a (#358): the project's semantic model, compiled against the lake at one exact snapshot,
as one unit, and with no authorization in it."""

from __future__ import annotations

import inspect
import io
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from orvex.server import app, commands, semantic_model
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.server.semantic_model import Loaded, SemanticModel, load_semantic
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


def model(source: str = "marts.consumption", metric: str = "SUM(consumption.kwh)") -> str:
    return json.dumps(
        {
            "version": "0.2.0.dev0",
            "name": "consumption",
            "datasets": [
                {
                    "name": "consumption",
                    "source": source,
                    "primary_key": ["consumer_id"],
                    "fields": [
                        {"name": c, "expression": {"sql": c}}
                        for c in ("consumer_id", "tariff", "kwh")
                    ],
                }
            ],
            "metrics": [{"name": "total_kwh", "expression": {"sql": metric}}],
            "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
        }
    )


POLICY = """\
version: 1
row_policies:
  - name: own_tariff
    dataset: consumption
    kind: predicate
    predicate: tariff = $tariff
"""


def ran(folder: Path, **files: str) -> int:
    """The project run once, then the files written; the snapshot the run committed."""
    full_project(folder)
    out = io.StringIO()
    assert commands.run(folder, IDENTITY, out) == 0, out.getvalue()
    write(folder, files)
    with opened(folder) as lake:
        return lake.current_snapshot().id


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


def loaded(folder: Path, snapshot: int | None = None) -> Loaded:
    with opened(folder) as lake:
        at = lake.current_snapshot().id if snapshot is None else snapshot
        return load_semantic(folder, lake, at, VENDORS, audience="builder")


def codes(found: Loaded) -> list[tuple[str, str]]:
    return [(p.code, p.file) for p in found.problems]


def test_a_valid_model_and_policies_compile_at_the_snapshot(tmp_path: Path) -> None:
    snapshot = ran(
        tmp_path, **{"semantic/consumption.ossie.json": model(), "security.yaml": POLICY}
    )
    found = loaded(tmp_path, snapshot)
    assert found.problems == ()
    assert isinstance(found.model, SemanticModel)
    assert found.model.snapshot == snapshot
    assert found.model.file == "semantic/consumption.ossie.json"
    assert found.model.policies is not None
    kwh = next(m for m in found.model.compiled.members if m.name == "KWH")
    assert found.model.compiled.require(kwh).typed.type.logical.datatype == "Decimal"


def test_a_project_without_a_semantic_folder_has_no_model_and_no_problem(tmp_path: Path) -> None:
    ran(tmp_path)
    assert loaded(tmp_path) == Loaded(None, ())


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (  # a source table the lake does not have
            {"semantic/m.ossie.json": model(source="marts.missing")},
            [("OSSIE_INVALID", "semantic/m.ossie.json")],
        ),
        (  # a type that does not agree: the sum of a text column
            {"semantic/m.ossie.json": model(metric="SUM(consumption.tariff)")},
            [("OSSIE_INVALID", "semantic/m.ossie.json")],
        ),
        (  # not Ossie
            {"semantic/m.ossie.yaml": "datasets: [unclosed\n"},
            [("OSSIE_INVALID", "semantic/m.ossie.yaml")],
        ),
        (  # two models
            {"semantic/a.ossie.json": model(), "semantic/b.ossie.json": model()},
            [("OSSIE_INVALID", "semantic/")],
        ),
        (  # an invalid model next to a valid policy file: nothing at all
            {
                "semantic/m.ossie.json": model(metric="SUM(consumption.tariff)"),
                "security.yaml": POLICY,
            },
            [("OSSIE_INVALID", "semantic/m.ossie.json")],
        ),
        (  # a missing table next to a valid policy file
            {"semantic/m.ossie.json": model(source="marts.missing"), "security.yaml": POLICY},
            [("OSSIE_INVALID", "semantic/m.ossie.json")],
        ),
        (  # a valid model with an invalid policy file: nothing at all
            {
                "semantic/m.ossie.json": model(),
                "security.yaml": POLICY.replace("dataset: consumption", "dataset: nowhere"),
            },
            [("POLICY_INVALID", "security.yaml")],
        ),
    ],
)
def test_any_problem_returns_no_model(
    tmp_path: Path, files: dict[str, str], expected: list[tuple[str, str]]
) -> None:
    ran(tmp_path, **files)
    found = loaded(tmp_path)
    assert found.model is None
    assert sorted(set(codes(found))) == expected
    assert all(p.message for p in found.problems)


def test_the_missing_source_names_the_dataset_and_the_snapshot(tmp_path: Path) -> None:
    snapshot = ran(tmp_path, **{"semantic/m.ossie.json": model(source="marts.missing")})
    (problem,) = loaded(tmp_path).problems
    assert "'consumption'" in problem.message
    assert "/datasets/0/source" in problem.message
    assert f"snapshot {snapshot}" in problem.message


def test_columns_come_from_the_snapshot_asked_not_the_newest(tmp_path: Path) -> None:
    first = ran(tmp_path, **{"semantic/m.ossie.json": model()})
    write(
        tmp_path,
        {  # a later run changes a column's type, then another drops the table
            "models/marts/consumption.sql": (
                "SELECT a.consumer_id, c.tariff, CAST(a.kwh AS DOUBLE) AS kwh "
                "FROM staging.active_consumers AS a JOIN staging.consumers AS c USING (consumer_id)"
            )
        },
    )
    assert commands.run(tmp_path, IDENTITY, io.StringIO()) == 0
    with opened(tmp_path) as lake:
        second = lake.current_snapshot().id
    (tmp_path / "models/marts/consumption.sql").unlink()
    assert commands.run(tmp_path, IDENTITY, io.StringIO(), drop_leftovers=True) == 0
    with opened(tmp_path) as lake:
        third = lake.current_snapshot().id

    def kwh(snapshot: int) -> str:
        found = loaded(tmp_path, snapshot)
        assert found.model is not None, found.problems
        member = next(m for m in found.model.compiled.members if m.name == "KWH")
        return str(found.model.compiled.require(member).typed.type.logical.datatype)

    assert first < second < third
    assert kwh(first) == "Decimal"
    assert kwh(second) == "Float"
    assert kwh(first) == "Decimal"  # the earlier snapshot still reads as it was
    assert codes(loaded(tmp_path, third)) == [("OSSIE_INVALID", "semantic/m.ossie.json")]


def test_validate_reports_semantic_problems_after_transform_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran(tmp_path, **{"semantic/m.ossie.json": model(source="marts.missing")})
    code = commands.validate(tmp_path, out := io.StringIO())
    assert code == 1
    assert out.getvalue().startswith("OSSIE_INVALID semantic/m.ossie.json reference at")
    write(tmp_path, {"semantic/m.ossie.json": model()})
    code = commands.validate(tmp_path, out := io.StringIO())
    assert (code, out.getvalue().strip()) == (0, "4 models, no problems.")


def test_validate_without_a_lake_says_the_model_is_not_checked(tmp_path: Path) -> None:
    full_project(tmp_path)
    write(tmp_path, {"semantic/m.ossie.json": model()})
    code = commands.validate(tmp_path, out := io.StringIO())
    assert code == 0
    assert out.getvalue().splitlines() == [
        "semantic model not checked: the lake has no snapshot yet",
        "4 models, no problems.",
    ]
    assert not (tmp_path / f".{SLUG}").exists()


def test_compiling_takes_no_caller_and_grants_nothing() -> None:
    """Authorization stays apart (Owner precision 3): no caller in, no permission out, and no
    HTTP route reaches the compiler."""
    assert list(inspect.signature(load_semantic).parameters) == [
        "folder",
        "lake",
        "snapshot",
        "vendor_names",
        "audience",
        "groups",
        "experimental",
        "connections",  # from `connections.yaml` (P2-29): never a caller
        "external",  # an enabled existing lake's tables (P2-30 PR 3): never a caller
    ]
    assert list(SemanticModel.__dataclass_fields__) == [
        "compiled",
        "policies",
        "snapshot",
        "file",
        "routing",  # every dataset's engine (P2-29): no permission
    ]
    assert "semantic_model" not in inspect.getsource(app)
    assert semantic_model.__name__ == "orvex.server.semantic_model"


def test_without_a_policy_file_the_model_has_no_policies(tmp_path: Path) -> None:
    ran(tmp_path, **{"semantic/m.ossie.json": model()})
    found = loaded(tmp_path)
    assert found.problems == ()
    assert found.model is not None
    assert found.model.policies is None


def _symlink_to_valid(path: Path) -> None:
    target = path.parent / "elsewhere.yaml"
    target.write_text(POLICY, encoding="utf-8")
    path.symlink_to(target)


def _unreadable(path: Path) -> None:
    path.write_text(POLICY, encoding="utf-8")
    path.chmod(0)


NOT_REGULAR = {
    "symlink-valid": _symlink_to_valid,
    "symlink-broken": lambda path: path.symlink_to(path.parent / "missing.yaml"),
    "directory": lambda path: path.mkdir(),
    "fifo": lambda path: os.mkfifo(path),
    "unreadable": _unreadable,
}


@pytest.mark.parametrize("make", NOT_REGULAR.values(), ids=NOT_REGULAR.keys())
def test_a_policy_file_that_is_not_a_readable_regular_file_returns_no_model(
    tmp_path: Path, request: pytest.FixtureRequest, make: Callable[[Path], None]
) -> None:
    """#410: anything at `security.yaml` but a regular file is POLICY_INVALID, never "no
    policies" (fail closed); a FIFO does not block the load."""
    if request.node.callspec.id == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads any file")
    ran(tmp_path, **{"semantic/m.ossie.json": model()})
    make(tmp_path / "security.yaml")
    found = loaded(tmp_path)
    assert found.model is None
    assert codes(found) == [("POLICY_INVALID", "security.yaml")]


def test_groups_named_in_local_mode_are_accepted_and_nobody_belongs(tmp_path: Path) -> None:
    """Owner decision on #358: local mode has no `access`, so the groups `security.yaml` names
    are taken as they are, and no caller is in any (no exemption applies). Given groups, the
    reader checks the names against them."""
    exempt = POLICY + "    exempt_groups: [management]\n"
    ran(tmp_path, **{"semantic/m.ossie.json": model(), "security.yaml": exempt})
    found = loaded(tmp_path)
    assert found.problems == ()
    assert found.model is not None
    assert commands.validate(tmp_path, io.StringIO()) == 0
    with opened(tmp_path) as lake:
        given = load_semantic(
            tmp_path,
            lake,
            lake.current_snapshot().id,
            VENDORS,
            audience="builder",
            groups=["finance"],
        )
    assert codes(given) == [("POLICY_INVALID", "security.yaml")]


def test_other_audiences_get_the_generic_message(tmp_path: Path) -> None:
    ran(tmp_path, **{"semantic/m.ossie.json": model(metric="SUM(consumption.tariff)")})
    with opened(tmp_path) as lake:
        found = load_semantic(tmp_path, lake, lake.current_snapshot().id, VENDORS, audience="other")
    (problem,) = found.problems
    assert problem.code == "OSSIE_INVALID"
    assert "/datasets" not in problem.message


def on_connection(name: str) -> str:
    """The model with its dataset on the connection `name` (P2-29)."""
    document = json.loads(model())
    document["datasets"][0]["custom_extensions"] = [
        extension({"element": "dataset", "connection": name})
    ]
    return json.dumps(document)


@pytest.mark.parametrize("name", ["archive", "nowhere"], ids=["route a", "unknown"])
def test_a_dataset_on_a_connection_is_a_problem_while_no_engine_is_direct_query(
    tmp_path: Path, name: str
) -> None:
    """Every connection this release knows is extracted into the lake (Route A), so no dataset
    is read on another engine: one naming a connection, known or not, is the model's problem."""
    ran(tmp_path, **{"semantic/m.ossie.json": on_connection(name)})
    with opened(tmp_path) as lake:
        found = load_semantic(
            tmp_path,
            lake,
            lake.current_snapshot().id,
            VENDORS,
            audience="builder",
            connections=load_project(tmp_path).connections,
        )
    assert found.model is None
    assert codes(found) == [("OSSIE_INVALID", "semantic/m.ossie.json")]
    assert found.problems[0].message.startswith("reference at /datasets/0/custom_extensions")
    code = commands.validate(tmp_path, out := io.StringIO())
    assert code == 1
    assert "OSSIE_INVALID semantic/m.ossie.json reference at /datasets/0" in out.getvalue()


def test_a_model_without_connections_is_on_the_managed_lake(tmp_path: Path) -> None:
    snapshot = ran(tmp_path, **{"semantic/m.ossie.json": model()})
    found = loaded(tmp_path, snapshot)
    assert found.model is not None
    assert not found.model.routing.external
