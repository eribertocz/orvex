"""Running a corpus case for real (P2-12, #311): its input CSVs loaded into a temporary
managed lake through `store`, its query answered through `orvex.semantic.execution.answer`,
and the result compared exactly with `expected/result.csv`.

This module composes `store` and `semantic` for the conformance runner, as `server` will for
the product (P2-27). Values travel as text: each CSV value is a bound parameter cast to the
column's type from the case's `.catalog.yaml`, so no value is converted on the way in, and the
result's `store/v1` JSON text is compared with the expected text, `NULL` apart from empty.
"""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import yaml
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    Parameter,
    ParameterType,
    StatementRequest,
)
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, Engine, answer
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.receipt import Key
from orvex.store.errors import StoreError
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake, Limits, open_lake

from conformance.runners import bi_platform_adapter as adapter

_PRODUCT = tomllib.loads((Path(__file__).parents[2] / "identity.toml").read_text("utf-8"))
VENDORS: tuple[str, ...] = (
    _PRODUCT["product"]["ossie_vendor_name"],
    *_PRODUCT["product"]["previous_vendor_names"],
)

INSTANT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
QUERIES: dict[str, Any] = json.loads(
    (Path(__file__).with_name("corpus_queries.json")).read_text("utf-8")
)["queries"]
# Test keys only: a corpus run seals nothing anyone relies on.
RESULT_KEY = Key("corpus-result", b"r" * 32)
SEAL_KEY = Key("corpus-seal", b"s" * 32)
_TYPES = {"TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ"}


@dataclass(frozen=True, slots=True)
class CaseRun:
    status: str  # pass, fail or not-run
    reason: str  # the capability not yet implemented, the mismatch, or the error code


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def catalog_of(model_path: Path) -> dict[str, dict[str, SourceColumn]]:
    written = yaml.safe_load(model_path.with_name(f"{model_path.stem}.catalog.yaml").read_text())
    return {
        source: {column: SourceColumn(*shape) for column, shape in columns.items()}
        for source, columns in adapter.translate_catalog(written).items()
    }


def compiled_case(model_path: Path) -> CompiledModel:
    vendors = VENDORS
    translated, _ = adapter.translate(
        cast(dict[str, Any], adapter.load_yaml(model_path)), vendors[0]
    )
    loaded = read(
        json.dumps(translated).encode(), f"semantic/{model_path.stem}.ossie.json", vendors
    )
    return compile_model(loaded, catalog_of(model_path), vendors)


def load_tables(
    lake: Lake, inputs: Sequence[Path], catalog: Mapping[str, Mapping[str, SourceColumn]]
) -> None:
    """Each `input/<table>.csv` as the lake table `<table>`, typed by the case's catalog."""
    with lake.transaction() as transaction:
        for path in inputs:
            columns = catalog[path.stem]
            definition = ", ".join(
                f"{_quote(name)} {_TYPES.get(c.type, c.type)}{'' if c.nullable else ' NOT NULL'}"
                for name, c in columns.items()
            )
            transaction.execute(
                StatementRequest(
                    kind="statement",
                    sql=f"CREATE TABLE {_quote(path.stem)} ({definition})",
                    parameters=[],
                )
            )
            for row in adapter.read_csv(path):
                names = list(row)
                values = ", ".join(
                    f"CAST($v{i} AS {_TYPES.get(columns[n].type, columns[n].type)})"
                    for i, n in enumerate(names)
                )
                transaction.execute(
                    StatementRequest(
                        kind="statement",
                        sql=f"INSERT INTO {_quote(path.stem)} "
                        f"({', '.join(_quote(n) for n in names)}) VALUES ({values})",
                        parameters=[
                            Parameter(name=f"v{i}", type=ParameterType("VARCHAR"), value=row[n])
                            for i, n in enumerate(names)
                        ],
                    )
                )


@contextmanager
def case_lake(
    inputs: Sequence[Path], catalog: Mapping[str, Mapping[str, SourceColumn]]
) -> Iterator[Lake]:
    with tempfile.TemporaryDirectory() as folder:
        location = LocalLakeLocation(kind="lake_location", catalog="local", path=folder)
        lake = open_lake(location, Limits("1GB", 2, 2, 0, 600_000))
        try:
            load_tables(lake, inputs, catalog)
            yield lake
        finally:
            lake.close()


def engine_for(lake: Lake, **change: Any) -> Engine:  # noqa: ANN401
    snapshot = lake.current_snapshot()
    arguments: dict[str, Any] = {
        "read": lake.read,
        "encoder": arrow_to_json,
        "snapshot": {"id": snapshot.id, "committed_at": snapshot.committed_at.isoformat()},
        "engine_version": version("duckdb"),
        "result_key": RESULT_KEY,
        "seal_key": SEAL_KEY,
        "instant": INSTANT,
        "limits": QueryLimits(detail_page_size=1000),
    }
    arguments.update(change)
    return Engine(**arguments)


def viewer_context(groups: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "kind": "security_context",
        "version": 1,
        "user": "corpus",
        "project": "corpus",
        "role": "viewer",
        "groups": sorted(set(groups)),
        "attributes": {},
        "permission_fingerprint": "sha256:" + "0" * 64,
    }


def rows_of(found: Answer) -> list[list[str | None]]:
    """The result's values as `store/v1` JSON text, positionally."""
    document = json.loads(found.result.wire(arrow_to_json))
    return [[None if v is None else str(v) for v in row] for row in document["rows"]]


# Refusals this runner names that the correctness engine's table does not hold.
PENDING = {
    "row_policy": "policy placement, P2-21",
    "expression": "an expression the DuckDB adapter does not write yet",
    "bucket": "a calendar bucket the DuckDB adapter does not write yet",
    "route": "another data route, P2-29",
}


# Cases this runner cannot exercise yet, for a reason outside the engine's own refusals:
# - their models compile only once their declarations exist (Owner decision of 2026-11-01, #283);
# - CASE-017's expected refusal is about a derived metric adding USD to BOB, which the adapter
#   does not translate yet; its written query asks for the two sums apart (corpus_queries.json).
NOT_YET: Mapping[str, str] = {
    "CASE-009": "the model compiles after P2-17 (#283)",
    "CASE-021": "the model compiles after P2-20 (#283)",
    "CASE-017": "the combined metric is translated with the units rule (P2-16)",
    # Its expected file holds the overall count beside the per-region ones: a total (P2-23).
    # Both are answered exactly, and proven in tests/conformance/test_period_end.py.
    "CASE-008": "the overall count is a total (P2-23)",
}


def capability(operation: str) -> str:
    """The refused capability and where it lands."""
    from orvex.semantic.correctness import SHAPES

    shape = SHAPES.get(operation)
    where = f"{shape.rule}, {shape.roadmap}" if shape else PENDING.get(operation, "a later issue")
    return f"{operation} ({where})"


def policies_of(model_path: Path) -> dict[str, Any]:
    """The case's own row policies, set aside by the adapter for P2-08, in `security-policies`
    v1 form: `:name` attributes become `$name`."""
    _, rows = adapter.translate(cast(dict[str, Any], adapter.load_yaml(model_path)), VENDORS[0])
    return {
        "version": 1,
        "row_policies": [
            {
                "name": row["name"],
                "dataset": row["dataset"],
                "kind": "predicate",
                "predicate": re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"$\1", row["predicate"]),
            }
            for row in rows
        ],
    }


def prepare(
    query: str, compiled: CompiledModel, policies: PolicyModel, context: Mapping[str, Any]
) -> None:
    """Every check `answer` makes before it reads, with no data: binding, `analyze`, `plan`
    and the adapter."""
    from orvex.semantic.adapters.duckdb import compile_plan
    from orvex.semantic.correctness import analyze
    from orvex.semantic.planning import plan
    from orvex.semantic.query.binding import bind_query

    groups = tuple(context["groups"])
    bound = bind_query(
        compiled,
        query,
        instant=INSTANT,
        limits=QueryLimits(detail_page_size=1000),
        hidden=lambda m: policies.protection(m, groups) == "hidden",
    )
    compile_plan(plan(analyze(bound, compiled, policies, context)))


def run_case(
    case: str,
    folder: Path,
    model_path: Path,
    expected_code: str | None,
) -> CaseRun:
    """`pass` only on an exact match (or the exact expected code); a refusal for a capability
    not yet implemented is `not-run`, naming it; anything else is `fail`."""
    if case in NOT_YET:
        return CaseRun("not-run", NOT_YET[case])
    try:
        compiled = compiled_case(model_path)
    except SemanticError as error:
        return _refused(error, expected_code)
    policies: PolicyModel = read_policies(
        yaml.safe_dump(policies_of(model_path)),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )
    # A declared rollup (`aggregate_table`) is a materialization (C8): this planner never reads
    # one, so it is not loaded.
    payloads = {
        e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
        for e in compiled.ossie.product_extensions
    }
    datasets = cast(Sequence[Mapping[str, Any]], compiled.ossie.document.get("datasets", ()))
    read_tables = {
        str(d.get("source", d["name"]))
        for at, d in enumerate(datasets)
        if "aggregate_table" not in payloads.get(f"/datasets/{at}", {})
    }
    inputs = sorted(p for p in (folder / "input").glob("*.csv") if p.stem in read_tables)
    query = json.dumps(QUERIES[case])
    try:  # every refusal comes before any data exists: a refused case loads nothing
        prepare(query, compiled, policies, viewer_context())
    except SemanticError as error:
        return _refused(error, expected_code)
    try:
        with case_lake(inputs, catalog_of(model_path)) as lake:
            found = answer(query, compiled, policies, viewer_context(), engine_for(lake))
    except SemanticError as error:
        return _refused(error, expected_code)
    except StoreError as error:
        return CaseRun("fail", f"{error.code}: {error.details}")
    if expected_code is not None:
        return CaseRun("fail", f"answered, but {expected_code} was expected")
    expected = adapter.read_csv(folder / "expected" / "result.csv")
    wanted = [list(row.values()) for row in expected]
    columns = found.result.envelope()["columns"]
    names = [c["name"] for c in columns]
    # The corpus names a bucketed column after its bucket ("month"); the engine after its member.
    accepted = [{c["name"], c.get("bucket", {}).get("unit", c["name"])} for c in columns]
    header = list(expected[0]) if expected else []
    if len(header) != len(names) or any(h not in a for h, a in zip(header, accepted, strict=True)):
        return CaseRun("fail", f"columns {names}")
    got = rows_of(found)
    dates = [c["encoding"] == "DATE" for c in found.result.envelope()["columns"]]
    return CaseRun("pass", "") if same(got, wanted, dates) else CaseRun("fail", f"rows {got}")


def same(got: list[list[str | None]], wanted: list[list[str | None]], dates: list[bool]) -> bool:
    """Equal as text. In a date column, the corpus writes a month as `YYYY-MM`; the engine
    answers the date the month starts on, `YYYY-MM-01`, which is the same value."""
    if len(got) != len(wanted):
        return False
    for row, expected in zip(got, wanted, strict=True):
        if len(row) != len(expected):
            return False
        for value, text, date in zip(row, expected, dates, strict=True):
            month = date and text is not None and re.fullmatch(r"[0-9]{4}-[0-9]{2}", text)
            if value != text and not (month and value == f"{text}-01"):
                return False
    return True


def _refused(error: SemanticError, expected_code: str | None) -> CaseRun:
    if expected_code is not None:
        if error.code == expected_code:
            return CaseRun("pass", error.code)
        if error.code != "UNSUPPORTED_OPERATION":
            return CaseRun("fail", f"{error.code} instead of {expected_code}")
    if error.code == "UNSUPPORTED_OPERATION":
        return CaseRun("not-run", capability(str(error.details["operation"])))
    if error.code == "MISSING_EXTENSION_DECLARATION":
        return CaseRun("not-run", f"declaration {error.details['declaration']} (#283)")
    return CaseRun("fail", error.code)
