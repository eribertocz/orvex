"""Test durations (#375): where the suite's time goes, and the record that balances its shards.

    python -m tools.ci.durations emit junit.xml               # in CI: one line per test, to the log
    python -m tools.ci.durations report LOGS_OR_JUNIT...      # the report, from logs or reports

`emit` prints each test of a JUnit report as one tab-separated line (`test-duration`, total
seconds, setup seconds, test id, fixtures), so a CI job's log carries its durations without an
artifact. `report` reads such logs or JUnit files (the `setup_s` and `fixtures` properties come
from `-p tools.ci.timing`) and writes:

- `--markdown PATH`: the slowest tests and files, the totals per module and per Python, and the
  setup time per fixture;
- `--record PATH`: every test's median total seconds, by test id, which `tools/ci/shard.py`
  uses to balance its parts.

A test seen on several Pythons or runs counts once per run in the totals and by its median in
the record. Nothing is estimated: a test with no measured time is not in the record.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.ci import shard
from tools.repository import REPOSITORY_ROOT

MARK = "test-duration"
# A GitHub Actions log line starts with its timestamp.
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ")
# `python-tests-3.15-part-3` in a log's name, or `junit-3.14.xml`.
_PYTHON = re.compile(r"(3\.\d+)")


@dataclass(frozen=True, slots=True)
class Timing:
    test: str  # the pytest node id: path::name[parameters]
    total: float
    setup: float
    fixtures: tuple[str, ...]
    python: str


def node_id(classname: str, name: str, root: Path | None = None) -> str:
    """The node id of a JUnit test case: `packages.store.tests.test_x` + `test_y` becomes
    `packages/store/tests/test_x.py::test_y` (a class becomes `::Class::test`). The module is
    the longest prefix that is a file of the repository; failing that, the last `test_` part."""
    parts = classname.split(".")
    base = REPOSITORY_ROOT if root is None else root
    for end in range(len(parts), 0, -1):
        if (base / ("/".join(parts[:end]) + ".py")).is_file():
            return "::".join(["/".join(parts[:end]) + ".py", *parts[end:], name])
    for end in range(len(parts), 0, -1):
        if parts[end - 1].startswith("test_") or end == 1:
            return "::".join(["/".join(parts[:end]) + ".py", *parts[end:], name])
    return f"{classname}::{name}"


def from_junit(path: Path, python: str = "") -> Iterator[Timing]:
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        if case.find("skipped") is not None:
            continue
        properties = {
            p.get("name", ""): p.get("value", "") for p in case.iter("property") if p.get("name")
        }
        yield Timing(
            node_id(case.get("classname", ""), case.get("name", "")),
            float(case.get("time") or 0),
            float(properties.get("setup_s") or 0),
            tuple(f for f in properties.get("fixtures", "").split(",") if f),
            python,
        )


def emit(path: Path) -> Iterator[str]:
    for timing in from_junit(path):
        fixtures = ",".join(timing.fixtures)
        yield f"{MARK}\t{timing.total:.4f}\t{timing.setup:.4f}\t{timing.test}\t{fixtures}"


def from_log(lines: Iterable[str], python: str = "") -> Iterator[Timing]:
    for raw in lines:
        line = _STAMP.sub("", raw.rstrip("\n"))
        if not line.startswith(MARK + "\t"):
            continue
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        _, total, setup, test, fixtures = fields
        yield Timing(
            test, float(total), float(setup), tuple(f for f in fixtures.split(",") if f), python
        )


def load(paths: Sequence[Path]) -> list[Timing]:
    found: list[Timing] = []
    for path in paths:
        match = _PYTHON.search(path.name)
        python = match.group(1) if match else ""
        if path.suffix == ".xml":
            found += from_junit(path, python)
        else:
            found += from_log(path.read_text("utf-8", "replace").splitlines(), python)
    return found


def module_of(test: str) -> str:
    parts = test.split("::", 1)[0].split("/")
    if parts[0] == "packages" and len(parts) > 1:
        return parts[1]
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def record(timings: Iterable[Timing]) -> dict[str, float]:
    """Every test's median total seconds: what the shards are balanced on."""
    seen: dict[str, list[float]] = defaultdict(list)
    for timing in timings:
        seen[shard.key(timing.test)].append(timing.total)
    return {test: round(statistics.median(values), 3) for test, values in sorted(seen.items())}


def markdown(timings: Sequence[Timing], title: str, top: int = 50) -> str:
    total = sum(t.total for t in timings)
    setup = sum(t.setup for t in timings)
    by_file: dict[str, float] = defaultdict(float)
    by_module: dict[str, float] = defaultdict(float)
    by_python: dict[str, float] = defaultdict(float)
    by_fixture: dict[str, list[float]] = defaultdict(list)
    for t in timings:
        by_file[t.test.split("::", 1)[0]] += t.total
        by_module[module_of(t.test)] += t.total
        by_python[t.python or "?"] += t.total
        for fixture in t.fixtures:
            by_fixture[fixture].append(t.setup)
    lines = [
        f"# {title}",
        "",
        f"- **Tests measured:** {len(timings):,} test runs, {len({t.test for t in timings}):,} "
        "distinct tests",
        f"- **Total test time:** {total / 60:,.1f} min, of which fixture setup "
        f"{setup / 60:,.1f} min ({setup / total:.0%})"
        if total
        else "- **Total test time:** 0",
        "",
        "## Per Python",
        "| Python | Minutes |",
        "|---|---|",
        *(f"| {p} | {s / 60:,.1f} |" for p, s in sorted(by_python.items())),
        "",
        "## Per module",
        "| Module | Minutes | Share |",
        "|---|---|---|",
        *(
            f"| {m} | {s / 60:,.1f} | {s / total:.0%} |"
            for m, s in sorted(by_module.items(), key=lambda i: -i[1])
        ),
        "",
        f"## The {top} slowest files",
        "| File | Minutes |",
        "|---|---|",
        *(
            f"| `{f}` | {s / 60:,.2f} |"
            for f, s in sorted(by_file.items(), key=lambda i: -i[1])[:top]
        ),
        "",
        f"## The {top} slowest tests (one run each)",
        "| Test | Python | Seconds | Setup s |",
        "|---|---|---|---|",
        *(
            f"| `{shard.key(t.test)}` | {t.python} | {t.total:,.1f} | {t.setup:,.1f} |"
            for t in sorted(timings, key=lambda t: -t.total)[:top]
        ),
        "",
        "## Setup time by fixture (each test's whole setup, counted for every fixture it uses)",
        "| Fixture | Tests | Setup minutes |",
        "|---|---|---|",
        *(
            f"| `{f}` | {len(v):,} | {sum(v) / 60:,.1f} |"
            for f, v in sorted(by_fixture.items(), key=lambda i: -sum(i[1]))[:25]
        ),
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.ci.durations")
    commands = parser.add_subparsers(dest="command", required=True)
    emitting = commands.add_parser("emit")
    emitting.add_argument("junit", type=Path)
    reporting = commands.add_parser("report")
    reporting.add_argument("paths", type=Path, nargs="+")
    reporting.add_argument("--markdown", type=Path)
    reporting.add_argument("--record", type=Path)
    reporting.add_argument("--title", default="Test durations")
    arguments = parser.parse_args(argv)
    if arguments.command == "emit":
        if not arguments.junit.is_file():
            return 0  # no report: the test step failed before writing one, and says why
        for line in emit(arguments.junit):
            print(line)
        return 0
    timings = load(arguments.paths)
    if not timings:
        print("durations: no measured test in the given files", file=sys.stderr)
        return 1
    if arguments.markdown:
        arguments.markdown.write_text(markdown(timings, arguments.title), "utf-8")
    if arguments.record:
        arguments.record.write_text(json.dumps(record(timings), indent=0) + "\n", "utf-8")
    if not (arguments.markdown or arguments.record):
        print(markdown(timings, arguments.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
