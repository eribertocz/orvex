"""Security conformance runner (P3-13, #447): every normative security scenario, on every engine
it lists, reported per case, engine and execution; the required `security-scenarios` check.

    python -m conformance.runners.security_scenarios [--engine managed|existing-ducklake|
        clickhouse ...] [--clickhouse host:port:user[:PASSWORD_ENV] ...]
        [--require-clickhouse 26.10,26.8] [--case SEC-…] [--json <file>]

**Logical records and executions** (Owner precision 3). One logical record per (case, listed
engine): 92 today. A case whose id is not its file's, or that lists no engine, an engine twice
or an unknown one, is one failed record of its own: never left out. Each record holds its
executions, one per engine instance:
- `managed`;
- `existing-ducklake/<catalog>`, for each catalog of the fixture's `lake.toml`;
- `clickhouse/<version>`, for each `--clickhouse` server, named by the `version()` it reports.

A required version (`--require-clickhouse`) that did not run makes the record
`EXECUTION_MISSING`. A record's status is its worst execution's.

**Statuses** of one execution:
- `pass`: the expectation and the control both hold, and no forbidden value appears anywhere
  the caller receives it (the result, its notices, the caller's view of the receipt, an error).
- `fail`, always, `pending` or not (Owner precision 1):
  - a wrong answer;
  - a wrong control;
  - a leak;
  - a failure of the infrastructure (an exception, an engine not reachable);
  - any refusal other than the documented limitation.
- `expected-fail`: the case is `pending` on a roadmap row of a later phase, and its run is
  **the documented limitation**: the request and its control are both refused, before any
  rows, with `UNSUPPORTED_OPERATION` (the engine's code for a capability not built yet), and no
  forbidden value appears. A pending case that passes is `STALE_PENDING`: its marker goes.
- `not-runnable`: a pending case whose request needs a surface that does not exist. The
  surface's absence from `http-api` v1 and from the server's routes is checked (precision 2).
  Once it exists the case is `STALE_NOT_RUNNABLE`: it must now run.
- `engine-pending`: the engine is in `pending_engines`, and the request, for the caller and the
  control, was refused there before any read.
- `not-applicable`: the case's change cannot exist on this instance's catalog (a SQLite catalog
  file on PostgreSQL, a PostgreSQL role on SQLite; `security_existing_lake.NotApplicable`).
- `not-reproduced`: a variant of the case's change this runner does not make, with why and the
  tests that show it (`security_existing_lake.NotReproduced`): never a pass, and listed.

A case's `variants` each run as an execution of their own, named after the instance
(`existing-ducklake/sqlite[by=hard_link]`).

**Protection** (the Owner's condition on #447), for each execution: `proven` when the case
passes, or, for a run refused for its limitation (`expected-fail` on detail pages,
`engine-pending`), when its aggregate counterpart passes (`security_counterparts`): the same
attack, end to end, on a query the engine supports, and its attack is reached by a query
(`security_counterparts.unreached`); `not-demonstrated` otherwise, with why; `not-applicable`
with that status. A forbidden value or a broken run in a counterpart is a `fail` like any.

The exit status is 0 only when every logical record has every required execution and none is
`fail`, `STALE_PENDING`, `STALE_NOT_RUNNABLE` or `EXECUTION_MISSING`.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import yaml
from orvex.server.route_permissions import PERMISSIONS

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from conformance.runners import security_http, security_sequences
from conformance.runners.security_cases import SEPARATOR, cases, later_row, requests
from conformance.runners.security_counterparts import counterpart, unreached
from conformance.runners.security_engines import (
    ClickHouseServer,
    Received,
    ask,
    default_clickhouse,
    served,
)
from conformance.runners.security_existing_lake import NotApplicable, NotReproduced
from tools.repository import REPOSITORY_ROOT

ENGINES = ("managed", "existing-ducklake", "clickhouse")
LIMITATION = "UNSUPPORTED_OPERATION"
FAILING = frozenset({"fail", "STALE_PENDING", "STALE_NOT_RUNNABLE", "EXECUTION_MISSING"})
WORST = ("EXECUTION_MISSING", "fail", "STALE_NOT_RUNNABLE", "STALE_PENDING", "expected-fail",
         "not-runnable", "engine-pending", "not-reproduced", "not-applicable", "pass")  # fmt: skip
# The surfaces not built yet, each by the request kinds that need it (precision 2).
SURFACES = {
    "/api/v1/pages": ("page", "page-query", "navigation", "draft-preview"),
    "/api/v1/exports": ("export", "export-start", "export-result"),
    "/mcp": ("mcp",),
    "/api/v1/semantic/values": ("possible-values",),
}
SURFACE_OF = {kind: path for path, kinds in SURFACES.items() for kind in kinds}
# Cases whose caller two policies withhold rows from, each with its own notice (#443).
REPEATED = {"SEC-DQ-001": 2, "SEC-MISSING-003": 2}


@dataclass
class Execution:
    instance: str  # managed, existing-ducklake/<catalog>, clickhouse/<version>
    status: str
    reason: str = ""
    # What the execution shows of the protection: `proven`, `not-demonstrated` or, for a change
    # its catalog cannot have, `not-applicable`; `shown` says how.
    protection: str = "not-demonstrated"
    shown: str = ""


@dataclass
class Record:
    case: str
    engine: str
    pending: str | None
    status: str = "pass"
    executions: list[Execution] = field(default_factory=list)


@dataclass(frozen=True)
class Instance:
    """One engine instance an execution runs on."""

    engine: str
    name: str  # as the report names it
    catalog: str = ""
    clickhouse: ClickHouseServer | None = None
    variant: tuple[tuple[str, str], ...] = ()  # one of the case's `variants`, run on its own


# --- what the caller receives, checked -------------------------------------------------------


def expected_rows(text: str) -> tuple[list[str], list[list[str | None]]]:
    lines = list(csv.reader(io.StringIO(text)))
    return lines[0], [[None if v == "NULL" else v for v in row] for row in lines[1:]]


def tokens_of(received: Received) -> set[str]:
    return {token for text in received.texts for token in SEPARATOR.split(text) if token}


def leaks(received: Received, forbidden: Iterable[str]) -> list[str]:
    """The forbidden values the caller received, whole, anywhere."""
    return sorted(set(forbidden) & tokens_of(received))


def _count(rows: list[list[str | None]] | None) -> str:
    return "no" if rows is None else str(len(rows))


def problems(received: Received, expect: Mapping[str, Any], label: str) -> list[str]:
    """How `received` differs from `expect`: none when it holds (leaks apart: `leaks`)."""
    found: list[str] = []
    outcome = expect["outcome"]
    if outcome not in {"rows", "zero-rows", "error", "contains", "absent"}:
        return [f"no outcome {outcome!r} for these requests"]
    if outcome == "error" and received.code != expect["code"]:
        found.append(f"code {received.code}, expected {expect['code']}")
    if outcome != "error" and received.code is not None:
        found.append(f"refused with {received.code}")
    if outcome == "rows":  # never the values: the report holds no data (shapes only)
        columns, rows = expected_rows(expect["rows"])
        if received.columns != columns:
            found.append("the columns differ")
        if received.rows != rows:
            found.append(f"the rows differ ({_count(received.rows)} for {len(rows)})")
    if outcome == "zero-rows" and received.rows != []:
        found.append(f"{_count(received.rows)} rows for none")
    tokens = tokens_of(received)
    if outcome == "contains":
        missing = sorted(set(expect.get("text", ())) - tokens)
        missing += sorted(set(expect.get("members", ())) - received.members)
        if missing:
            found.append(f"{len(missing)} expected values missing")
    if outcome == "absent" and set(expect["members"]) & received.members:
        found.append("a member the catalog must omit is there")
    named = [expect["notice"]] * REPEATED.get(label, 1) if "notice" in expect else []
    if outcome == "contains":
        named = [t for t in expect.get("text", ()) if t in received.notices]
    if received.notices != named:
        found.append(f"notices {received.notices}")
    return found


# --- one execution ---------------------------------------------------------------------------


def asked(case: Mapping[str, Any], found: sf.Fixture, instance: Instance) -> tuple[Any, Any]:
    """The case's last answer and its control's last answer on `instance`."""
    request, control = case["request"], case["control"]
    if request["kind"] == "sequence":
        if security_http.over_http(request):
            if instance.engine != "managed":
                raise RuntimeError("the HTTP steps are served on the managed lake only")
            last = security_http.run(found, request, True)[-1]
            changes = not control.get("without_changes")
            checked = security_http.run(found, request, changes, control["caller"])[-1]
            return last, checked
        last_step = {**request["steps"][-1], "caller": control["caller"]}
        controlled = control.get("request") or {
            **request,
            "steps": [*request["steps"][:-1], last_step],
        }
        with served(found, instance.engine, instance.catalog, instance.clickhouse) as place:
            *_, last = security_sequences.run(found, place, request, True)
        with served(found, instance.engine, instance.catalog, instance.clickhouse) as place:
            changes = not control.get("without_changes")
            *_, checked = security_sequences.run(found, place, controlled, changes)
        return last, checked
    with served(found, instance.engine, instance.catalog, instance.clickhouse) as place:
        last = ask(found, place, request, case["caller"])
        checked = ask(found, place, control.get("request", request), control["caller"])
    return last, checked


def refused_before_reading(
    case: Mapping[str, Any], found: sf.Fixture, instance: Instance
) -> list[str]:
    """For a pending engine: every request, for the caller and the control, refused with the
    limitation before that engine read anything."""
    request = case["request"]
    steps = [s["request"] for s in request["steps"]] if request["kind"] == "sequence" else [request]
    each = [s for s in steps if s["kind"] in {"semantic-query", "catalog", "explain"}]
    found_problems: list[str] = []
    with served(found, instance.engine, instance.catalog, instance.clickhouse) as place:
        for caller in (case["caller"], case["control"]["caller"]):
            for one in each:
                got = ask(found, place, one, caller)
                if got.code != LIMITATION or got.rows is not None:
                    found_problems.append(f"answered {got.code} with {_count(got.rows)} rows")
        if place.reads:
            found_problems.append("the engine read before refusing")
    return found_problems


def surfaces_present() -> set[str]:
    """The not-built surfaces that now exist in `http-api` v1 or in the server's routes."""
    text = (REPOSITORY_ROOT / "contracts/http-api/v1/openapi.yaml").read_text("utf-8")
    paths = set(yaml.safe_load(text).get("paths", {})) | set(PERMISSIONS)  # the server's routes
    return {s for s in SURFACES if any(p == s or p.startswith(s + "/") for p in paths)}


def execute(name: str, case: Mapping[str, Any], instance: Instance, present: set[str]) -> Execution:
    pending = case.get("pending")
    kinds = {r["kind"] for r in requests(case["request"])}
    needed = sorted({SURFACE_OF[k] for k in kinds if k in SURFACE_OF})
    if needed:
        if pending is None:
            return Execution(instance.name, "fail", f"needs {needed}, which do not exist")
        if set(needed) & present:
            return Execution(instance.name, "STALE_NOT_RUNNABLE", f"{needed} exist: run it")
        if later_row(pending) is None:
            return Execution(instance.name, "fail", f"pending {pending!r} is not a later row")
        return Execution(instance.name, "not-runnable", f"needs {needed} ({pending})")
    if instance.variant:
        case = with_variant(case, dict(instance.variant))
    try:
        found = sf.fixture(case["fixture"], ce.VENDORS[0])
    except Exception as error:  # noqa: BLE001 - a broken run is a failure, never a skip
        return Execution(instance.name, "fail", _broken(error))
    if instance.engine in case.get("pending_engines", {}):
        return _engine_pending(name, case, found, instance)
    return _checked(name, case, found, instance)


def _engine_pending(
    name: str, case: Mapping[str, Any], found: sf.Fixture, instance: Instance
) -> Execution:
    """Refused before reading, and the protection shown by the aggregate counterpart, or why it
    is not shown (the report's `protection`)."""
    where = case["pending_engines"][instance.engine]
    try:
        wrong = refused_before_reading(case, found, instance)
    except Exception as error:  # noqa: BLE001 - a broken run is a failure, never a skip
        return Execution(instance.name, "fail", _broken(error))
    if wrong:
        return Execution(instance.name, "fail", "; ".join(wrong))
    pending = Execution(instance.name, "engine-pending", f"refused before reading ({where})")
    return _shown(name, case, found, instance, pending)


def _shown(
    name: str, case: Mapping[str, Any], found: sf.Fixture, instance: Instance, limited: Execution
) -> Execution:
    """A run refused for its limitation, with what its aggregate counterpart shows of the
    protection: `proven`, `not-demonstrated` (and why), or a failure for a leak."""
    made = counterpart(case, found)
    if made is None:
        return replace(limited, protection="not-demonstrated", shown="no aggregate counterpart")
    shown = _checked(name, made, found, instance)
    if shown.status == "pass" and (why := unreached(case)) is not None:
        return replace(limited, protection="not-demonstrated", shown=f"unreached: {why}")
    if shown.status == "pass":
        return replace(limited, protection="proven", shown="aggregate counterpart passes")
    if shown.status in {"not-applicable", "not-reproduced"}:
        return replace(limited, protection=shown.protection, shown=shown.reason)
    if shown.reason.startswith(("leaked", "infrastructure")):  # a failure wherever it shows
        return Execution(instance.name, "fail", f"aggregate counterpart: {shown.reason}")
    return replace(limited, protection="not-demonstrated", shown=f"aggregate: {shown.reason}")


def _broken(error: Exception) -> str:
    text = str(error).splitlines()[0][:200] if str(error) else ""
    return f"infrastructure: {type(error).__name__} {text}"


def _checked(
    name: str, case: Mapping[str, Any], found: sf.Fixture, instance: Instance
) -> Execution:
    """The case run on `instance`, its answer and its control checked."""
    pending = case.get("pending")
    try:
        last, checked = asked(case, found, instance)
    except NotApplicable as error:  # the attack does not exist on this catalog
        return Execution(instance.name, "not-applicable", str(error), "not-applicable")
    except NotReproduced as error:  # a variant this runner does not make: never a pass
        return Execution(instance.name, "not-reproduced", str(error), "not-demonstrated")
    except Exception as error:  # noqa: BLE001 - a broken run is a failure, never a skip
        return Execution(instance.name, "fail", _broken(error))
    leaked = leaks(last, case["expect"].get("forbidden", ()))  # the control may show them
    if leaked:  # counted, never named: the report holds no forbidden value
        return Execution(instance.name, "fail", f"leaked {len(leaked)} forbidden values")
    wrong = problems(last, case["expect"], name)
    control = problems(checked, {**case["control"], "forbidden": ()}, f"{name} control")
    if pending is None:
        if wrong or control:
            return Execution(
                instance.name, "fail", "; ".join(wrong + [f"control: {c}" for c in control])
            )
        return Execution(instance.name, "pass", protection="proven")
    if later_row(pending) is None:
        return Execution(instance.name, "fail", f"pending {pending!r} is not a later row")
    if not wrong and not control:
        return Execution(instance.name, "STALE_PENDING", f"passes: remove `pending: {pending}`")
    limited = all(r.code == LIMITATION and r.rows is None for r in (last, checked))
    if limited:
        expected = Execution(instance.name, "expected-fail", f"{LIMITATION} ({pending})")
        return _shown(name, case, found, instance, expected)
    reasons = wrong + [f"control: {c}" for c in control]
    return Execution(
        instance.name, "fail", "pending, but not its limitation: " + "; ".join(reasons)
    )


# --- the run ---------------------------------------------------------------------------------


def instances(
    engine: str, case: Mapping[str, Any], servers: Sequence[tuple[str, ClickHouseServer]]
) -> list[Instance]:
    if engine == "managed":
        found = [Instance("managed", "managed")]
    elif engine == "existing-ducklake":
        settings = sf.fixture(case["fixture"], ce.VENDORS[0]).lake_settings()
        found = [
            Instance(engine, f"existing-ducklake/{c}", catalog=c) for c in settings["catalogs"]
        ]
    else:
        found = [Instance(engine, f"clickhouse/{v}", clickhouse=s) for v, s in servers]
    variants = [tuple(sorted((str(k), str(v)) for k, v in item.items()))
                for item in case.get("variants", ())]  # fmt: skip
    if not variants:
        return found
    return [
        replace(one, name=f"{one.name}[{','.join(f'{k}={v}' for k, v in variant)}]",
                variant=variant)
        for one in found for variant in variants
    ]  # fmt: skip


def with_variant(case: Mapping[str, Any], variant: Mapping[str, str]) -> dict[str, Any]:
    """The case with one variant's fields in each of its lake changes (`variants`)."""
    made: dict[str, Any] = copy.deepcopy(dict(case))
    for request in (made["request"], made["control"].get("request")):
        if request is None or request["kind"] != "sequence":
            continue
        for step in request["steps"]:
            if step["request"]["kind"] == "lake-change":
                step["request"]["change"] = {**step["request"]["change"], **variant}
    made.pop("variants", None)
    return made


def run(
    selected: Mapping[str, Mapping[str, Any]],
    engines: Iterable[str],
    servers: Sequence[tuple[str, ClickHouseServer]],
    required: Sequence[str],
    present: set[str] | None = None,
) -> list[Record]:
    """Every logical record of `selected` on `engines`, with its executions."""
    present = surfaces_present() if present is None else present
    wanted = set(engines)
    records = []
    for name, case in sorted(selected.items()):
        pending = case.get("pending")
        listed = list(case.get("engines", ()))
        wrong = _inventory(name, case, listed)
        if wrong:  # a case the run cannot place is a failure, never left out
            record = Record(name, ",".join(listed) or "none", None)
            record.executions.append(Execution("inventory", "fail", wrong))
            record.status = "fail"
            records.append(record)
            continue
        for engine in listed:
            if engine not in wanted:
                continue
            record = Record(name, engine, None if pending is None else str(pending))
            for instance in instances(engine, case, servers):
                record.executions.append(execute(name, case, instance, present))
            if engine == "clickhouse":
                ran = {version for version, _ in servers}
                for version in required:
                    if not any(r == version or r.startswith(version + ".") for r in ran):
                        record.executions.append(
                            Execution(f"clickhouse/{version}", "EXECUTION_MISSING", "not run")
                        )
                if not record.executions:
                    record.executions.append(
                        Execution("clickhouse", "EXECUTION_MISSING", "no ClickHouse server")
                    )
            record.status = min((e.status for e in record.executions), key=WORST.index)
            records.append(record)
    return records


def _inventory(name: str, case: Mapping[str, Any], listed: list[str]) -> str:
    """Why the case cannot be placed in the report: none when it can (precision 3, AC1)."""
    if case.get("id") != name:
        return f"its id {case.get('id')!r} is not its file's"
    if not listed:
        return "it lists no engine"
    if len(set(listed)) != len(listed):
        return "it lists an engine twice"
    unknown = sorted(set(listed) - set(ENGINES))
    return f"it lists unknown engines {unknown}" if unknown else ""


def servers_of(arguments: Sequence[str]) -> list[tuple[str, ClickHouseServer]]:
    """Each `--clickhouse host:port:user[:PASSWORD_ENV]`, with the version it reports."""
    found = []
    for argument in arguments:
        host, port, user, *rest = argument.split(":")
        password = os.environ.get(rest[0], "") if rest else ""
        server = ClickHouseServer(host, int(port), user, password)
        found.append((server.version(), server))
    return found


def report(records: Sequence[Record]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    shown: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
        for execution in record.executions:
            shown[execution.protection] = shown.get(execution.protection, 0) + 1
    return {
        "kind": "security_scenarios_report",
        "version": 1,
        "logical_records": len(records),
        "executions": sum(len(r.executions) for r in records),
        "statuses": dict(sorted(counts.items())),
        "protection": dict(sorted(shown.items())),
        "records": [asdict(r) for r in records],
    }


def _line(execution: Execution) -> str:
    shown = f": {execution.shown}" if execution.shown else ""
    told = f"{execution.instance}: {execution.status} {execution.reason}"
    return f"{told} [{execution.protection}{shown}]"


def main(argv: Sequence[str] | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="security_scenarios")
    parser.add_argument("--engine", action="append", choices=ENGINES)
    parser.add_argument("--clickhouse", action="append", default=[])
    parser.add_argument("--require-clickhouse", default="")
    parser.add_argument("--case", action="append")
    parser.add_argument("--json")
    asked_for = parser.parse_args(argv)
    engines = asked_for.engine or list(ENGINES)
    try:
        servers = servers_of(asked_for.clickhouse)
    except (OSError, RuntimeError, ValueError) as error:
        out(f"SECURITY_RUNNER_FAILED: a ClickHouse server cannot be reached: {error}")
        return 1
    if "clickhouse" in engines and not servers and os.environ.get("SEMANTIC_TEST_CLICKHOUSE"):
        default = default_clickhouse()
        servers = [(default.version(), default)]
    selected = {n: c for n, c in cases().items() if not asked_for.case or n in set(asked_for.case)}
    required = [v for v in asked_for.require_clickhouse.split(",") if v]
    records = run(selected, engines, servers, required)
    document = report(records)
    if asked_for.json:
        with open(asked_for.json, "w", encoding="utf-8") as file:
            json.dump(document, file, indent=2)
            file.write("\n")
    for record in records:
        if record.status != "pass":
            reasons = "; ".join(_line(e) for e in record.executions)
            out(f"{record.status:<18} {record.case} on {record.engine}: {reasons}")
    out(f"{document['logical_records']} logical records, {document['executions']} executions: "
        + ", ".join(f"{k} {v}" for k, v in document["statuses"].items()))  # fmt: skip
    out("protection, by execution: "
        + ", ".join(f"{k} {v}" for k, v in document["protection"].items()))  # fmt: skip
    return 1 if any(r.status in FAILING for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
