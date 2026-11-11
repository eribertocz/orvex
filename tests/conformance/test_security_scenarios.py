"""#435: the normative security scenarios are well formed, cover every required scenario, and
cannot pass by refusing everything.

A case is checked against its own schema and against the contracts it uses. Its fixture must be
a valid project: a model that compiles and policies that read. Each code it expects must be
registered or reserved. Its control must show the data the caller must not get. Its `pending`
must name where the behavior is built. Running the cases on the engines is #447.
"""

from __future__ import annotations

import csv
import io
import json
import re
import tomllib
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies.model import read_policies
from orvex.server.identity import load_identity

from tools.checks import error_codes
from tools.repository import REPOSITORY_ROOT

SECURITY = REPOSITORY_ROOT / "conformance/security"
CONTRACTS = REPOSITORY_ROOT / "contracts"
IDENTITY = load_identity()
VENDORS = (IDENTITY.ossie_vendor_name, *IDENTITY.previous_vendor_names)
# A request carries a semantic-query body under one of these keys.
BODIES = ("body", "alternate")


def schema(path: Path) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(path.read_text("utf-8")))


@cache
def validators() -> dict[str, jsonschema.Draft202012Validator]:
    return {
        "scenario": schema(SECURITY / "scenario.schema.json"),
        "caller": schema(CONTRACTS / "security-context/v1/schema.json"),
        "query": schema(CONTRACTS / "semantic-query/v1/schema.json"),
    }


def files() -> list[Path]:
    return sorted((SECURITY / "scenarios").rglob("*.yaml"))


@cache
def cases() -> dict[str, dict[str, Any]]:
    return {path.stem: yaml.safe_load(path.read_text("utf-8")) for path in files()}


def requests(request: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """`request` and every request nested in it (the steps of a sequence)."""
    yield request
    for step in request.get("steps", []):
        yield from requests(step["request"])


def callers(case: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield case["caller"]
    yield case["control"]["caller"]
    for request in requests(case["request"]):
        for step in request.get("steps", []):
            yield step["caller"]


def queries(case: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every semantic-query body of a case: those a caller sends as such."""
    roots = [case["request"]] + (
        [case["control"]["request"]] if "request" in case["control"] else []
    )
    for root in roots:
        assert isinstance(root, Mapping)
        for request in requests(root):
            for key in BODIES:
                if key in request:
                    yield request[key]
            if request.get("kind") == "mcp" and "query" in request.get("arguments", {}):
                yield request["arguments"]["query"]


def defined_codes() -> set[str]:
    return set(error_codes.contract_codes(REPOSITORY_ROOT)) | set(
        error_codes.reserved_codes(REPOSITORY_ROOT)
    )


def control_shows(case: Mapping[str, Any]) -> str:
    control = case["control"]
    shown = [control.get("rows", "")]
    shown += [*control.get("members", []), *control.get("pages", []), *control.get("text", [])]
    return "\n".join(shown)


def current_phase() -> int:
    return error_codes.current_phase(REPOSITORY_ROOT)


def roadmap_ids() -> set[str]:
    text = (REPOSITORY_ROOT / "docs/roadmap.md").read_text("utf-8")
    return set(re.findall(r"^\| (P[0-9]+-[0-9]{2}[a-z]?) \|", text, re.MULTILINE))


def where(error: jsonschema.ValidationError) -> str:
    """The instance path of a schema error, and its message."""
    return f"{'/'.join(map(str, error.absolute_path))}: {error.message[:120]}"


def problems(case: Mapping[str, Any], name: str, area: str) -> list[str]:
    """Every rule of #435 item 4 this case breaks."""
    found = [
        f"SCENARIO_INVALID {name} {where(e)}" for e in validators()["scenario"].iter_errors(case)
    ]
    if found:
        return found
    if case["id"] != name or case["area"] != area:
        found.append(f"SCENARIO_MISPLACED {name}: id {case['id']} in area {case['area']}")
    for who in callers(case):
        found += [
            f"SCENARIO_CALLER_INVALID {name} {where(e)}"
            for e in validators()["caller"].iter_errors(who)
        ]
    for body in queries(case):
        found += [
            f"SCENARIO_QUERY_INVALID {name} {where(e)}"
            for e in validators()["query"].iter_errors(body)
        ]
    codes = defined_codes()
    expect = case["expect"]
    for code in (expect.get("code"), expect.get("notice")):
        if code is not None and code not in codes:
            found.append(f"SCENARIO_CODE_UNKNOWN {name}: {code} is neither registered nor reserved")
    if expect["outcome"] != "rows" and not any(
        f in control_shows(case) for f in expect["forbidden"]
    ):
        found.append(f"SCENARIO_CONTROL_SHOWS_NOTHING {name}: the control shows no forbidden value")
    for key in ("rows",):
        for owner in (expect, case["control"]):
            if key in owner:
                rows = list(csv.reader(io.StringIO(owner[key])))
                if len({len(row) for row in rows}) != 1:
                    found.append(f"SCENARIO_ROWS_RAGGED {name}: every row needs every column")
    pending = case.get("pending")
    if isinstance(pending, str):
        phase = int(pending[1:].split("-")[0])
        if pending not in roadmap_ids():
            found.append(f"SCENARIO_PENDING_UNKNOWN {name}: {pending} is not in the roadmap")
        elif phase <= current_phase():
            found.append(
                f"SCENARIO_PENDING_STARTED {name}: phase P{phase} has started; name its issue"
            )
    return found


@pytest.mark.parametrize("path", files(), ids=lambda p: p.stem)
def test_each_case_is_well_formed(path: Path) -> None:
    case = yaml.safe_load(path.read_text("utf-8"))
    assert problems(case, path.stem, path.parent.name) == []


def test_the_suite_has_every_area_and_unique_ids() -> None:
    assert len(cases()) == len(files())
    areas = {case["area"] for case in cases().values()}
    loaded = json.loads((SECURITY / "scenario.schema.json").read_text("utf-8"))
    assert areas == set(loaded["properties"]["area"]["enum"])


def coverage() -> dict[str, dict[str, list[str]]]:
    loaded: dict[str, dict[str, list[str]]] = tomllib.loads(
        (SECURITY / "coverage.toml").read_text("utf-8")
    )
    return loaded


def coverage_problems(table: Mapping[str, Mapping[str, list[str]]], known: set[str]) -> list[str]:
    found = []
    for section in ("roadmap", "adr_0027"):
        for item, ids in table.get(section, {}).items():
            if not ids:
                found.append(f"SCENARIO_UNCOVERED {section}: {item}")
            found += [f"SCENARIO_COVERAGE_UNKNOWN {item}: {i}" for i in ids if i not in known]
    listed = {i for section in table.values() for ids in section.values() for i in ids}
    found += [f"SCENARIO_NOT_IN_COVERAGE {i}" for i in sorted(known - listed)]
    return found


def test_every_required_scenario_has_a_case() -> None:
    table = coverage()
    assert coverage_problems(table, set(cases())) == []
    assert len(table["roadmap"]) == 12  # the roadmap row's ten, plus the Owner's two
    assert len(table["adr_0027"]) == 18  # every item of ADR-0027 §6's list


def test_every_direct_query_item_names_its_adr_entry() -> None:
    for case in cases().values():
        assert (case["area"] == "direct-query") == ("adr_0027" in case), case["id"]


# --- the fixtures are real projects ---------------------------------------------------------


def fixture_catalog(
    folder: Path,
) -> tuple[dict[str, dict[str, SourceColumn]], dict[str, list[str]]]:
    tables = tomllib.loads((folder / "tables.toml").read_text("utf-8"))["tables"]
    catalog = {
        name: {c: SourceColumn(t, nullable) for c, t, nullable in table["columns"]}
        for name, table in tables.items()
    }
    for name, table in tables.items():
        rows = list(csv.reader(io.StringIO((folder / table["file"]).read_text("utf-8"))))
        assert rows[0] == [c for c, _, _ in table["columns"]], name
        assert all(len(row) == len(rows[0]) for row in rows), name
    managed = {
        n: [c for c, _, _ in t["columns"]] for n, t in tables.items() if n.startswith("security.")
    }
    return catalog, managed


@pytest.mark.parametrize("name", ["utility", "direct-query"])
def test_each_fixture_compiles_and_its_policies_read(name: str) -> None:
    folder = SECURITY / "fixtures" / name
    catalog, managed = fixture_catalog(folder)
    text = (folder / "model.ossie.json").read_text("utf-8").replace("{{vendor}}", VENDORS[0])
    compiled = compile_model(read(text.encode(), "model.ossie.json", VENDORS), catalog, VENDORS)
    groups = {
        g
        for case in cases().values()
        if case["fixture"] == name
        for who in callers(case)
        for g in who["groups"]
    }
    policies = (folder / "security.yaml").read_text("utf-8")
    read_policies(
        policies,
        compiled,
        groups=groups | {"collections", "engineering", "management", "operations"},
        managed_tables=managed,
    )


# --- the rules refuse what they must (R1-R5) ------------------------------------------------


def planted(name: str = "SEC-ROWS-001", **changes: object) -> tuple[dict[str, Any], str, str]:
    case = json.loads(json.dumps(cases()[name]))
    for path, value in changes.items():
        *parents, last = path.split("__")
        node = case
        for key in parents:
            node = node[key]
        if value is None:
            node.pop(last, None)
        else:
            node[last] = value
    return case, name, case["area"]


def codes_of(found: list[str]) -> list[str]:
    return [problem.split()[0] for problem in found]


def test_r1_a_case_without_forbidden_or_control_or_a_telling_control_fails() -> None:
    assert codes_of(problems(*planted(expect__forbidden=None))) == ["SCENARIO_INVALID"]
    assert codes_of(problems(*planted(control=None))) == ["SCENARIO_INVALID"]
    refused = planted("SEC-COLUMNS-002", control__rows="consumers.consumer_id\nC1\n")
    assert codes_of(problems(*refused)) == ["SCENARIO_CONTROL_SHOWS_NOTHING"]


def test_r2_a_code_neither_raised_nor_reserved_fails() -> None:
    found = problems(*planted("SEC-COLUMNS-002", expect__code="MEMBER_SECRETLY_THERE"))
    assert codes_of(found) == ["SCENARIO_CODE_UNKNOWN"]


def test_r3_an_uncovered_item_or_an_orphan_case_fails() -> None:
    table = coverage()
    table["adr_0027"]["a NULL in a deny-list mapping"] = []
    assert "SCENARIO_UNCOVERED" in codes_of(coverage_problems(table, set(cases())))
    assert "SCENARIO_NOT_IN_COVERAGE" in codes_of(coverage_problems(table, set(cases())))


def test_r4_a_case_breaking_its_contracts_fails_with_the_path() -> None:
    found = problems(*planted(caller__role="owner"))
    assert codes_of(found) == ["SCENARIO_CALLER_INVALID"]
    assert "role" in found[0]
    found = problems(*planted(request__body__mode="sql"))
    assert codes_of(found)[0] == "SCENARIO_QUERY_INVALID"


def test_r5_a_pending_without_a_reference_or_on_a_started_phase_fails() -> None:
    assert codes_of(problems(*planted("SEC-EXPORT-001", pending="later"))) == ["SCENARIO_INVALID"]
    started = f"P{current_phase()}-01"
    assert codes_of(problems(*planted("SEC-EXPORT-001", pending=started))) == [
        "SCENARIO_PENDING_STARTED"
    ]
    assert codes_of(problems(*planted("SEC-EXPORT-001", pending="P8-99"))) == [
        "SCENARIO_PENDING_UNKNOWN"
    ]
