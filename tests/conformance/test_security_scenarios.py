"""#435: the normative security scenarios are well formed, cover every required scenario, and
cannot pass by refusing everything.

A case is checked against its own schema and against the contracts it uses. Its fixture must be
a valid project, committed: a model that compiles and policies that read. Each code it expects
must be registered or reserved. Its control must show a value the caller must not get, and its
expected result must show none. Its `pending` must name where the behavior is built. A case
already enforced at policy validation is proven here against the real reader. Running the cases
on the engines is #447.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import tomllib
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
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
# How a forbidden value is matched: whole, against tokens split on every other character.
SEPARATOR = re.compile(r"[^A-Za-z0-9_./*-]+")


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


def tokens(owner: Mapping[str, Any], keys: tuple[str, ...]) -> set[str]:
    """Every whole value `owner` shows under `keys`: cells, column names, items and their tokens."""
    shown: set[str] = set()
    if "rows" in keys and "rows" in owner:
        shown |= {cell for row in csv.reader(io.StringIO(owner["rows"])) for cell in row}
    shown |= {item for key in keys if key != "rows" for item in owner.get(key, [])}
    return shown | {token for value in shown for token in SEPARATOR.split(value) if token}


def control_shows(case: Mapping[str, Any]) -> set[str]:
    return tokens(case["control"], ("rows", "members", "pages", "text"))


def expected_shows(case: Mapping[str, Any]) -> set[str]:
    """What the expected result shows; an `absent` outcome lists what must not be there."""
    expect = case["expect"]
    if expect["outcome"] == "absent":
        return set()
    return tokens(expect, ("rows", "members", "pages", "text"))


@cache
def pending_issues() -> dict[int, str]:
    loaded = tomllib.loads((SECURITY / "pending-issues.toml").read_text("utf-8"))
    return {int(number): row for number, row in loaded["issues"].items()}


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
    if not set(expect["forbidden"]) & control_shows(case):
        found.append(f"SCENARIO_CONTROL_SHOWS_NOTHING {name}: the control shows no forbidden value")
    if shown := sorted(set(expect["forbidden"]) & expected_shows(case)):
        found.append(f"SCENARIO_EXPECT_SHOWS_FORBIDDEN {name}: {', '.join(shown)}")
    if "code_defined_by" in expect and expect["code_defined_by"] != case.get("pending"):
        found.append(f"SCENARIO_CODE_OWNER_MISMATCH {name}: code_defined_by is not the pending")
    for key in ("rows",):
        for owner in (expect, case["control"]):
            if key in owner:
                rows = list(csv.reader(io.StringIO(owner[key])))
                if len({len(row) for row in rows}) != 1:
                    found.append(f"SCENARIO_ROWS_RAGGED {name}: every row needs every column")
    return found + pending_problems(case.get("pending"), name)


def pending_problems(pending: int | str | None, name: str) -> list[str]:
    """A roadmap ID must be of a later phase; an issue must be one of this phase's issues."""
    if pending is None:
        return []
    if isinstance(pending, int):
        row = pending_issues().get(pending)
        if row is None:
            return [f"SCENARIO_PENDING_UNKNOWN {name}: #{pending} is not in pending-issues.toml"]
        if row not in roadmap_ids() or int(row[1:].split("-")[0]) != current_phase():
            return [f"SCENARIO_PENDING_UNKNOWN {name}: #{pending} maps to {row}, not this phase"]
        return []
    if pending not in roadmap_ids():
        return [f"SCENARIO_PENDING_UNKNOWN {name}: {pending} is not in the roadmap"]
    phase = int(pending[1:].split("-")[0])
    if phase <= current_phase():
        return [f"SCENARIO_PENDING_STARTED {name}: phase P{phase} has started; name its issue"]
    return []


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


@cache
def fixture(name: str) -> tuple[CompiledModel, dict[str, list[str]], frozenset[str]]:
    """The fixture's compiled model, its managed-lake tables, and every group its cases name."""
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
    return (
        compiled,
        managed,
        frozenset(groups | {"collections", "engineering", "management", "operations"}),
    )


def read_fixture_policies(name: str, document: Mapping[str, Any]) -> None:
    compiled, managed, groups = fixture(name)
    read_policies(yaml.safe_dump(dict(document)), compiled, groups=groups, managed_tables=managed)


def fixture_policies(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (SECURITY / "fixtures" / name / "security.yaml").read_text("utf-8")
    )
    return loaded


@pytest.mark.parametrize("name", ["utility", "direct-query"])
def test_each_fixture_compiles_and_its_policies_read(name: str) -> None:
    read_fixture_policies(name, fixture_policies(name))


def test_every_fixture_file_is_committed() -> None:
    ignored = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--", str(SECURITY)],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert ignored == []


# A policy change a case makes, applied to the fixture's policy document.
def changed_policies(name: str, change: Mapping[str, Any]) -> dict[str, Any]:
    document = fixture_policies(name)
    if change["op"] == "replace_policy":
        (rule,) = [r for r in document["row_policies"] if r["name"] == change["policy"]]
        rule["predicate"] = change["predicate"]
    else:
        rules = [r for r in document["column_policies"] if r["member"] != change["member"]]
        added = {"member": change["member"], "default": change["default"]}
        document["column_policies"] = [
            *rules,
            added | ({"mask": change["mask"]} if "mask" in change else {}),
        ]
    return document


def policy_changes(case: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found = []
    for request in requests(case["request"]):
        found += [request["change"]] if request.get("kind") == "lake-change" else []
        found += [step["change"] for step in request.get("steps", []) if "change" in step]
    return [c for c in found if c["op"] in {"replace_policy", "replace_column_policy"}]


ENFORCED_AT_VALIDATION = sorted(
    name for name, case in cases().items() if policy_changes(case) and "pending" not in case
)


@pytest.mark.parametrize("name", ENFORCED_AT_VALIDATION)
def test_a_case_enforced_at_validation_is_refused_by_the_real_reader(name: str) -> None:
    case = cases()[name]
    (change,) = policy_changes(case)
    with pytest.raises(SemanticError) as raised:
        read_fixture_policies(case["fixture"], changed_policies(case["fixture"], change))
    assert raised.value.code == case["expect"]["code"]


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
    # A rows case is held to the same rule: a control showing only the caller's rows proves nothing.
    own = planted(control__rows="readings.zone_id,total_kwh\nZ1,30.75\n")
    assert codes_of(problems(*own)) == ["SCENARIO_CONTROL_SHOWS_NOTHING"]
    leaked = planted(expect__rows="readings.zone_id,total_kwh\nZ1,30.75\nZ2,30.00\n")
    assert codes_of(problems(*leaked)) == ["SCENARIO_EXPECT_SHOWS_FORBIDDEN"]
    # Matching is whole: `2` is not shown by `20.25` or `Z2`, and a value is one token.
    whole = planted(expect__forbidden=["2"], control__rows="readings.zone_id,total_kwh\nZ2,20.25\n")
    assert codes_of(problems(*whole)) == ["SCENARIO_CONTROL_SHOWS_NOTHING"]
    assert codes_of(problems(*planted(expect__forbidden=["Z2 Z3"]))) == ["SCENARIO_INVALID"]


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
    assert codes_of(problems(*planted("SEC-ROWS-001", pending=99999))) == [
        "SCENARIO_PENDING_UNKNOWN"
    ]
    owner = planted("SEC-REVOKE-002", pending=444)
    assert codes_of(problems(*owner)) == ["SCENARIO_CODE_OWNER_MISMATCH"]
