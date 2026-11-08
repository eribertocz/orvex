"""Test-time trends and the visibility report (#377, #373 §6), for every `main` build.

    python -m tools.ci.trends --history test-trends.json --logs LOGS --run ID --workflow NAME \\
        --sha SHA --date DATE --conclusion CONCLUSION --url URL [--summary PATH]

`test-trends.yml` runs it after each `python` (profile `full`) and `property-nightly` (profile
`nightly`) run on `main`. It reads that run's job logs (one file per job, named after the job)
with `tools/ci/durations.py`, appends one entry to the history (the total test time, the slowest
part, the tests and the skipped tests by reason), and writes the report: the latest `full` and
`nightly` results, the skipped tests by reason, the tests in no profile and any sustained rise.

A rise is flagged only when it is sustained: each of the last `WINDOW` comparable runs (same
profile, successful) exceeds the median of the `BASELINE` comparable runs before them by more
than `THRESHOLD`, and at least `MINIMUM` such earlier runs exist. One noisy run, or a dip inside
the window, flags nothing. A flag is a warning for the next session; it never fails a build.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tools.ci import checks, durations
from tools.repository import REPOSITORY_ROOT

PROFILES = {"python": "full", "property-nightly": "nightly"}
METRICS = {"total_s": "total test time", "slowest_s": "slowest part"}
WINDOW = 5
BASELINE = 10
MINIMUM = 5
THRESHOLD = 0.2
KEPT = 400  # entries kept in the history: months of builds
DISCOVERED = "governance-scripts"  # the check whose unittest discovery runs the scripts' tests


@dataclass(frozen=True)
class Entry:
    run: int
    profile: str
    sha: str
    date: str
    conclusion: str
    url: str
    tests: int
    total_s: float
    slowest_part: str
    slowest_s: float
    skipped: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Rise:
    profile: str
    metric: str
    message: str


def measure(
    logs: Path, run: int, workflow: str, sha: str, date: str, conclusion: str, url: str
) -> Entry:
    """This run's entry, from its job logs (`<job name>.txt`)."""
    if workflow not in PROFILES:
        raise ValueError(f"unknown workflow {workflow!r}: expected one of {sorted(PROFILES)}")
    parts: dict[str, float] = defaultdict(float)
    skipped: Counter[str] = Counter()
    timed = 0
    for log in sorted(logs.glob("*.txt")):
        lines = log.read_text("utf-8", "replace").splitlines()
        timings = list(durations.from_log(lines))
        if timings:
            parts[log.stem] = sum(t.total for t in timings)
            timed += len(timings)
        skipped += durations.skipped_from_log(lines)
    slowest = max(parts, key=lambda name: parts[name]) if parts else ""
    return Entry(
        run=run,
        profile=PROFILES[workflow],
        sha=sha,
        date=date,
        conclusion=conclusion,
        url=url,
        tests=timed + sum(skipped.values()),
        total_s=round(sum(parts.values()), 1),
        slowest_part=slowest,
        slowest_s=round(parts[slowest], 1) if slowest else 0.0,
        skipped=dict(sorted(skipped.items(), key=lambda i: -i[1])),
    )


def rises(history: Sequence[Entry]) -> list[Rise]:
    """The sustained rises of each profile's metrics (see the module's description)."""
    found = []
    for profile in sorted({e.profile for e in history}):
        comparable = [e for e in history if e.profile == profile and e.conclusion == "success"]
        for metric, label in METRICS.items():
            values = [float(getattr(e, metric)) for e in comparable if getattr(e, metric) > 0]
            recent, earlier = values[-WINDOW:], values[:-WINDOW][-BASELINE:]
            if len(recent) < WINDOW or len(earlier) < MINIMUM:
                continue
            base = statistics.median(earlier)
            if all(value > base * (1 + THRESHOLD) for value in recent):
                found.append(
                    Rise(
                        profile,
                        metric,
                        f"{profile}: the {label} rose over the last {WINDOW} builds, from a "
                        f"median of {base / 60:,.1f} min to {min(recent) / 60:,.1f} to "
                        f"{max(recent) / 60:,.1f} min (+{min(recent) / base - 1:.0%} or more)",
                    )
                )
    return found


def unprofiled(root: Path, discovered: str | None = None) -> list[str]:
    """The tracked test files no profile runs: outside pytest's `testpaths` and the scripts'
    unittest discovery. Must be none (#373 §2)."""
    configuration = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    roots = [f"{p}/" for p in configuration["tool"]["pytest"]["ini_options"]["testpaths"]]
    if discovered is None:
        scripts = next(c for c in checks.load(root) if c.name == DISCOVERED)
        discovered = scripts.directory or ""
    tracked = subprocess.run(
        ["git", "ls-files", "*test_*.py"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return [t for t in tracked if not t.startswith((*roots, f"{discovered}/"))]


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:,.1f}"


def report(history: Sequence[Entry], outside: Sequence[str]) -> str:
    lines = [
        "# Test trends",
        "",
        "## Latest results",
        "| Profile | Result | Run | Commit | Date | Tests | Total min | Slowest part | Min |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for profile in ("full", "nightly"):
        latest = next((e for e in reversed(history) if e.profile == profile), None)
        if latest is None:
            lines.append(f"| {profile} | no run recorded | | | | | | | |")
            continue
        lines.append(
            f"| {profile} | {latest.conclusion} | [{latest.run}]({latest.url}) | "
            f"`{latest.sha[:7]}` | {latest.date} | {latest.tests:,} | "
            f"{_minutes(latest.total_s)} | {latest.slowest_part} | {_minutes(latest.slowest_s)} |"
        )
    found = rises(history)
    lines += ["", "## Trends"]
    if found:
        lines += [f"- **Sustained rise:** {rise.message}" for rise in found]
    else:
        lines.append(
            f"No sustained rise: no metric exceeded its baseline by {THRESHOLD:.0%} over the "
            f"last {WINDOW} comparable builds."
        )
    lines += ["", "## Tests not run in the latest full build, by reason"]
    latest_full = next((e for e in reversed(history) if e.profile == "full"), None)
    if latest_full is None or not latest_full.skipped:
        lines.append("None.")
    else:
        lines += ["| Tests | Reason |", "|---|---|"]
        lines += [f"| {count:,} | {reason} |" for reason, count in latest_full.skipped.items()]
    lines += ["", "## Tests in no profile"]
    if outside:
        lines += ["These test files run in no profile:", *(f"- `{path}`" for path in outside)]
    else:
        lines.append("Tests in no profile: none.")
    return "\n".join(lines) + "\n"


def load(path: Path) -> list[Entry]:
    if not path.is_file():
        return []
    return [Entry(**item) for item in json.loads(path.read_text("utf-8"))]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.ci.trends")
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--conclusion", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--summary", type=Path)
    arguments = parser.parse_args(argv)
    history = load(arguments.history)
    if all(e.run != arguments.run for e in history):
        history.append(
            measure(
                arguments.logs,
                arguments.run,
                arguments.workflow,
                arguments.sha,
                arguments.date,
                arguments.conclusion,
                arguments.url,
            )
        )
    history = history[-KEPT:]
    arguments.history.write_text(json.dumps([asdict(e) for e in history], indent=1) + "\n", "utf-8")
    text = report(history, unprofiled(REPOSITORY_ROOT))
    if arguments.summary:
        with arguments.summary.open("a", encoding="utf-8") as summary:
            summary.write(text)
    else:
        print(text)
    for rise in rises(history):
        print(f"::warning title=Sustained test-time rise::{rise.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
