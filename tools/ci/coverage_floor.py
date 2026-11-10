"""Branch coverage of the critical sources against their floors (#429), for one Python version.

    python -m tools.ci.coverage_floor <data-folder> --python 3.15

`<data-folder>` holds one folder per test part (`coverage-<python>-<part>`), each with the
part's `.coverage.*` data or, when that part did not run the whole suite, a `not-measured` file.
Every part must be there. When no part measured, coverage was not measured (a pull request's
selection) and the check passes with a notice; a mix fails, and so does an unmeasured push to
`main`, which always runs the whole suite (a broken selection never turns the gate off).

The parts' data are combined, and every Python file under `critical_sources`
(`tools/ci/checks.toml`) must be in it (instrumented) with a branch coverage at or above its
floor in `tools/ci/coverage-floor.toml`. The workflow runs this once per Python version, so
each version must reach every floor: the lower of the two decides, as the floors require. A
module one percent or more above its floor is reported as a candidate for a reviewed raise,
never failed. Missing, empty or unreadable data fails (fail closed).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

from tools.ci.parts import PARTS
from tools.repository import REPOSITORY_ROOT

FLOORS = Path("tools/ci/coverage-floor.toml")
NOT_MEASURED = "not-measured"
_SQLITE = b"SQLite format 3\x00"  # every coverage data file is an SQLite database


class CoverageError(Exception):
    """The coverage data cannot be checked."""


def critical_sources(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Every Python file under `critical_sources`, repository-relative, sorted."""
    document = tomllib.loads((root / "tools/ci/checks.toml").read_text("utf-8"))
    listed = document.get("profiles", {}).get("critical_sources", [])
    if not listed:
        raise CoverageError("tools/ci/checks.toml lists no critical_sources")
    found: set[str] = set()
    for entry in listed:
        path = root / entry
        if path.is_file() and path.suffix == ".py":
            found.add(entry)
        elif path.is_dir():
            found |= {f.relative_to(root).as_posix() for f in path.rglob("*.py")}
        else:
            raise CoverageError(f"critical source {entry} is not a Python file or folder")
    return sorted(found)


def floors(root: Path = REPOSITORY_ROOT) -> dict[str, int]:
    try:
        listed = tomllib.loads((root / FLOORS).read_text("utf-8")).get("floors", {})
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CoverageError(f"cannot read {FLOORS}: {error}") from error
    if not all(
        isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100 for v in listed.values()
    ):
        raise CoverageError(f"{FLOORS}: every floor is a whole percent from 0 to 100")
    return dict(listed)


def part_files(folder: Path, python: str, parts: int | None = None) -> list[Path] | None:
    """The parts' data files, or None when no part measured; a missing or mixed part fails."""
    parts = PARTS if parts is None else parts
    data: list[Path] = []
    unmeasured = 0
    for part in range(parts):
        here = folder / f"coverage-{python}-{part}"
        if not here.is_dir():
            raise CoverageError(f"part {part} of Python {python} left no coverage folder")
        files = sorted(here.glob(".coverage*"))
        if (here / NOT_MEASURED).is_file():
            unmeasured += 1
            if files:
                raise CoverageError(f"part {part} of Python {python} is both measured and not")
        elif not files:
            raise CoverageError(f"part {part} of Python {python} left no coverage data")
        data += files
    if unmeasured == parts:
        return None
    if unmeasured:
        raise CoverageError(f"{unmeasured} of {parts} parts of Python {python} were not measured")
    return data


def measure(files: Sequence[Path], root: Path) -> dict[str, float]:
    """The branch coverage (percent) of each file the suite imported, from the combined data.
    A file never imported is left out: `source` would report it at 0%, but it was not measured."""
    import coverage  # only where coverage is measured: the dev group installs it

    for file in files:
        with file.open("rb") as stream:
            if stream.read(16) != _SQLITE:
                raise CoverageError(f"unreadable coverage data: {file.name} is not a data file")
    with tempfile.TemporaryDirectory() as scratch:
        combined = Path(scratch) / ".coverage"
        work = coverage.Coverage(data_file=str(combined), config_file=str(root / "pyproject.toml"))
        try:
            work.combine([str(f) for f in files], keep=True, strict=True)
            work.save()
            data = work.get_data()
            # `source` records a never-imported file with no line: it was not measured.
            imported = {_relative(n, root) for n in data.measured_files() if data.lines(n)}
            report = Path(scratch) / "coverage.json"
            work.json_report(outfile=str(report), ignore_errors=False)
        except coverage.exceptions.CoverageException as error:
            raise CoverageError(f"unreadable coverage data: {error}") from error
        files_found = json.loads(report.read_text("utf-8")).get("files", {})
    if not files_found or not imported:
        raise CoverageError("the coverage data is empty")
    found = {
        _relative(name, root): float(entry["summary"]["percent_covered"])
        for name, entry in files_found.items()
    }
    return {name: value for name, value in found.items() if name in imported}


def _relative(name: str, root: Path) -> str:
    path = Path(name)
    return (path.relative_to(root) if path.is_absolute() else path).as_posix()


def judge(
    measured: dict[str, float], sources: Sequence[str], floor: dict[str, int]
) -> tuple[list[str], list[str]]:
    """(failures, candidates for a raise)."""
    failures = [
        f"COVERAGE_FLOOR_UNKNOWN {m}: not a critical source"
        for m in sorted(set(floor) - set(sources))
    ]
    candidates = []
    for source in sources:
        if source not in floor:
            failures.append(f"COVERAGE_FLOOR_MISSING {source}: no floor")
        if source not in measured:
            failures.append(f"COVERAGE_NOT_INSTRUMENTED {source}: never imported by the suite")
            continue
        if source not in floor:
            continue
        value = measured[source]
        if value < floor[source]:
            failures.append(f"COVERAGE_BELOW_FLOOR {source}: {value:.2f}% < {floor[source]}%")
        elif math.floor(value) >= floor[source] + 1:
            candidates.append(f"{source}: {value:.2f}% (floor {floor[source]}%)")
    return failures, candidates


def floors_text(measured: dict[str, float], sources: Sequence[str]) -> str:
    """`coverage-floor.toml` at the measured values, rounded down: for a reviewed raise."""
    rows = [f'"{s}" = {math.floor(measured[s])}' for s in sources if s in measured]
    return "[floors]\n" + "\n".join(rows) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--measured", type=Path, help="write the measured floors here")
    arguments = parser.parse_args(argv)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f"## Coverage of the critical sources, Python {arguments.python}", ""]
    try:
        files = part_files(arguments.folder, arguments.python)
        if files is None:
            if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":  # unknown: fail closed
                raise CoverageError("a push runs the whole suite, but no part measured coverage")
            print(
                f"::notice::coverage not measured on Python {arguments.python}: not the whole suite"
            )
            return 0
        sources = critical_sources()
        measured = measure(files, REPOSITORY_ROOT)
        failures, candidates = judge(measured, sources, floors())
    except CoverageError as error:
        print(f"::error::COVERAGE_DATA_INVALID: {error}")
        return 1
    if arguments.measured:
        arguments.measured.write_text(floors_text(measured, sources), "utf-8")
    lines += [f"- `{s}`: {measured[s]:.2f}%" for s in sources if s in measured]
    if candidates:
        lines += ["", "Above their floor by one percent or more (raise by review):", ""]
        lines += [f"- {c}" for c in candidates]
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
    for source in sources:
        if source in measured:
            print(f"coverage {measured[source]:.2f}% {source}")
    for failure in failures:
        print(f"::error::{failure}")
    for candidate in candidates:
        print(f"::notice::coverage above its floor: {candidate}")
    print(f"coverage: {len(sources)} critical sources, {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
