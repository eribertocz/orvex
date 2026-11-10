"""Run the mandatory checks locally (#240, #375 profiles): CI only confirms.

    python -m tools.ci.verify fast   # while working: quick checks and the changed modules' tests
    python -m tools.ci.verify pr     # before pushing: + critical checks and tests, + what the
                                     # change affects (tools/ci/impact.py), in parallel parts
    python -m tools.ci.verify full   # every check this machine can run: CI runs it on every
                                     # pull request; locally for a tier A pull request

`full` refuses a working tree with uncommitted changes, checks out HEAD in a temporary
directory, builds one environment per Python of the CI matrix from its lock file, installs the
store's DuckDB extensions (tools/ci/duckdb_extensions.py) and runs every check
`tools/ci/checks.toml` marks local, at the same time when they are independent. The summary
lists what passed, what failed, what was not run and why, and the tests that did not run grouped
by their skip reason: a test that did not run is never counted as passed. Any failure makes the
exit status non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from tools.ci import checks, impact
from tools.repository import REPOSITORY_ROOT

TESTS = "python-tests"
PIP = "pip==26.3"  # the version CI installs first (python.yml)
CACHE = Path.home() / ".cache" / "verify-environments"
# The checks `fast` runs: quick ones, each well under a minute.
FAST = (
    "product-name",
    "boundaries",
    "contracts-validate",
    "contracts-generated",
    "governance-scripts",
    "ruff-format",
    "ruff-check",
    "mypy-modules",
    "mypy-tests",
)


@dataclass(frozen=True)
class Step:
    name: str
    python: str
    directory: str | None
    command: tuple[str, ...]
    junit: str | None = None
    variables: tuple[tuple[str, str], ...] = ()  # set for this step only (a part's PYTEST_SHARD)


@dataclass
class Result:
    name: str
    status: str  # "passed", "failed" or "not run"
    detail: str = ""
    seconds: float = 0.0
    skipped: Counter[str] = field(default_factory=Counter)
    tests: int = 0


def parts(workers: int, pythons: int) -> int:
    """Test parts per Python: the workers shared by the Pythons (at least one each). Each part
    is a process with the suite's own DuckDB limits; the count is chosen from measurement
    (benchmarks/test-durations.md), not from the CPU count alone."""
    return max(1, workers // max(1, pythons))


def _parted(
    name: str, python: str, command: tuple[str, ...], junit: Path, total: int
) -> list[Step]:
    """The test step as `total` disjoint parts (`PYTEST_SHARD`), each with its own report."""
    if total == 1:
        return [Step(name, python, None, command, str(junit))]
    found = []
    for index in range(total):
        report = junit.with_name(f"{junit.stem}-part-{index}.xml")
        text = command[-1].replace(f"--junitxml={junit}", f"--junitxml={report}")
        found.append(
            Step(
                f"{name}-part-{index}",
                python,
                None,
                (*command[:-1], text),
                str(report),
                (("PYTEST_SHARD", f"{index}/{total}"),),
            )
        )
    return found


def plan(
    root: Path, pythons: Sequence[str], junit_dir: Path, total: int = 1
) -> tuple[list[Step], list[Result]]:
    """The steps of `full`, and the checks it does not run with their reasons. `total`: the
    test parts per Python."""
    steps: list[Step] = []
    skipped: list[Result] = []
    for check in checks.load(root):
        if not check.local:
            skipped.append(Result(check.name, "not run", check.reason or ""))
        elif check.name == TESTS:
            for python in check.pythons:
                junit = junit_dir / f"junit-{python}.xml"
                command = _with_report(checks.script(root, check), str(junit))
                steps += _parted(f"{TESTS}-{python}", python, ("bash", "-c", command), junit, total)
        else:
            steps.append(
                Step(
                    check.name,
                    pythons[0],
                    check.directory,
                    ("bash", "-eo", "pipefail", "-c", checks.script(root, check)),
                )
            )
    for job, reason in checks.not_run_jobs(root):
        skipped.append(Result(job, "not run", reason))
    return steps, skipped


def _with_report(script: str, junit: str) -> str:
    """The test step's own script, writing its report where this run reads it."""
    if script.count("--junitxml=junit.xml") != 1:
        raise RuntimeError("the test step no longer writes --junitxml=junit.xml once")
    return script.replace("--junitxml=junit.xml", f"--junitxml={junit}")


def changed_tests(root: Path, files: Iterable[str]) -> list[str]:
    """The test directories `fast` runs for the changed files: each changed package's tests,
    and the repository's tests for anything outside the packages."""
    selected: set[str] = set()
    for name in files:
        parts = Path(name).parts
        if len(parts) >= 2 and parts[0] == "packages":
            tests = Path("packages") / parts[1] / "tests"
            if (root / tests).is_dir():
                selected.add(tests.as_posix())
        elif parts and parts[0] in ("tests", "tools", "conformance"):
            selected.add("tests")
    return sorted(selected)


def skipped_tests(junit: Path) -> tuple[int, Counter[str]]:
    """How many tests the junit report holds, and its skipped tests counted by reason."""
    reasons: Counter[str] = Counter()
    total = 0
    for case in ElementTree.parse(junit).getroot().iter("testcase"):
        total += 1
        skip = case.find("skipped")
        if skip is not None:
            reasons[skip.get("message") or "no reason given"] += 1
    return total, reasons


def run_step(step: Step, cwd: Path, environment: dict[str, str]) -> Result:
    start = time.monotonic()
    directory = cwd / step.directory if step.directory else cwd
    process = subprocess.run(
        step.command,
        cwd=directory,
        env={**environment, **dict(step.variables)},
        capture_output=True,
        text=True,
    )
    result = Result(step.name, "passed" if process.returncode == 0 else "failed")
    result.seconds = time.monotonic() - start
    if process.returncode != 0:
        output = (process.stdout + process.stderr).strip().splitlines()
        result.detail = "\n".join(output[-40:])
    if step.junit:
        junit = Path(step.junit)
        if junit.is_file():
            result.tests, result.skipped = skipped_tests(junit)
        elif result.status == "passed":
            result.status, result.detail = "failed", "the test run wrote no junit report"
    return result


def summary(results: Sequence[Result]) -> tuple[str, int]:
    """The printed summary and the exit status: non-zero when anything failed."""
    lines = []
    for status in ("passed", "failed", "not run"):
        group = [r for r in results if r.status == status]
        if not group:
            continue
        lines.append(f"{status.upper()} ({len(group)})")
        for result in group:
            timing = f" [{result.seconds:.0f}s]" if result.seconds else ""
            reason = f": {result.detail}" if status == "not run" and result.detail else ""
            lines.append(f"  {result.name}{timing}{reason}")
            if result.tests:
                ran = result.tests - sum(result.skipped.values())
                lines.append(f"    {ran} of {result.tests} tests ran; not run, by reason:")
                for reason_text, count in sorted(result.skipped.items(), key=lambda i: -i[1]):
                    lines.append(f"      {count:5d}  {reason_text}")
            if status == "failed" and result.detail:
                lines.extend(f"    | {line}" for line in result.detail.splitlines())
    failed = any(r.status == "failed" for r in results)
    lines.append(
        "VERIFY FAILED" if failed else "VERIFY PASSED (not run items above are not passes)"
    )
    return "\n".join(lines), 1 if failed else 0


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def uncommitted(root: Path) -> list[str]:
    return [line for line in _git(root, "status", "--porcelain").splitlines() if line]


def _interpreter(python: str) -> str:
    found = shutil.which(f"python{python}")
    if found is None:
        raise RuntimeError(f"Python {python} (of the CI matrix) is not installed")
    return found


def environment(root: Path, python: str, checkout: Path) -> Path:
    """A virtual environment for `python` built as CI builds it: pip, then the lock file
    (kept between runs while the lock file is unchanged and its install completed), then the
    checkout's packages installed editable."""
    lock = root / ("pylock.py314.toml" if python == "3.14" else "pylock.toml")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    venv = CACHE / f"{python}-{digest}"
    interpreter = venv / "bin" / "python"
    complete = venv / ".lock-installed"
    if not complete.is_file():
        shutil.rmtree(venv, ignore_errors=True)
        venv.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([_interpreter(python), "-m", "venv", venv], check=True)
        subprocess.run([interpreter, "-m", "pip", "install", "-q", PIP], check=True)
        subprocess.run([interpreter, "-m", "pip", "install", "-q", "-r", lock], check=True)
        complete.write_text(lock.name)
    for package in sorted((checkout / "packages").iterdir()):
        if (package / "pyproject.toml").is_file():
            subprocess.run(
                [
                    interpreter,
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "--no-deps",
                    "--no-build-isolation",
                    "-e",
                    package,
                ],
                check=True,
            )
    return venv


def _environment_for(venv: Path) -> dict[str, str]:
    variables = dict(os.environ)
    variables["PATH"] = f"{venv / 'bin'}{os.pathsep}{variables.get('PATH', '')}"
    variables["VIRTUAL_ENV"] = str(venv)
    variables.pop("PYTEST_SHARD", None)
    return variables


def full(
    root: Path,
    workers: int,
    run: Callable[[Step, Path, dict[str, str]], Result] = run_step,
    total: int | None = None,
) -> int:
    if dirty := uncommitted(root):
        print("verify full: commit or remove these changes first (it checks HEAD):")
        print("\n".join(f"  {line}" for line in dirty))
        return 2
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="verify-") as scratch:
        base = Path(scratch)
        pythons = next(c.pythons for c in checks.load(root) if c.name == TESTS)
        steps, results = plan(root, pythons, base, total or parts(workers, len(pythons)))
        # One checkout per Python, so the runs at the same time share no files.
        checkouts = {}
        for python in pythons:
            checkout = base / f"checkout-{python}"
            _git(root, "worktree", "add", "--detach", "--quiet", str(checkout), "HEAD")
            checkouts[python] = checkout
        try:
            venvs = {}
            for python in pythons:
                venvs[python] = environment(root, python, checkouts[python])
                extensions = subprocess.run(
                    [venvs[python] / "bin" / "python", "-m", "tools.ci.duckdb_extensions"],
                    cwd=checkouts[python],
                    capture_output=True,
                    text=True,
                )
                print(extensions.stdout.strip() or extensions.stderr.strip())
                if extensions.returncode != 0:
                    results.append(
                        Result(
                            f"duckdb-extensions-{python}",
                            "failed",
                            extensions.stdout + extensions.stderr,
                        )
                    )
            # The long test runs first, so the quick checks fill the other workers meanwhile.
            ordered = sorted(steps, key=lambda s: s.junit is None)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results.extend(
                    pool.map(
                        lambda s: run(s, checkouts[s.python], _environment_for(venvs[s.python])),
                        ordered,
                    )
                )
        finally:
            for checkout in checkouts.values():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(checkout)], cwd=root, check=False
                )
    text, status = summary(results)
    print(text)
    print(f"verify full: {time.monotonic() - started:.0f}s")
    return status


def fast(
    root: Path, workers: int, run: Callable[[Step, Path, dict[str, str]], Result] = run_step
) -> int:
    """The quick checks and the changed modules' tests, in the working tree as it is."""
    base = _git(root, "merge-base", "HEAD", "origin/main").strip()
    changed = _git(root, "diff", "--name-only", base).splitlines()
    changed += _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    steps = [
        Step(c.name, "", c.directory, ("bash", "-eo", "pipefail", "-c", checks.script(root, c)))
        for c in checks.load(root)
        if c.name in FAST and c.local
    ]
    variables = dict(os.environ)
    variables["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{variables.get('PATH', '')}"
    with tempfile.TemporaryDirectory(prefix="verify-") as scratch:
        if tests := changed_tests(root, changed):
            junit = str(Path(scratch) / "junit.xml")
            command = (sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}", *tests)
            steps.append(Step(TESTS, "", None, command, junit))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda s: run(s, root, variables), steps))
    text, status = summary(results)
    print(text)
    return status


def profiles(root: Path) -> dict[str, list[str]]:
    """The critical checks and tests, and the nightly regressions (`checks.toml`, `[profiles]`)."""
    document = tomllib.loads((root / checks.LIST).read_text(encoding="utf-8"))
    found = document.get("profiles", {})
    return {
        "checks": [str(name) for name in found.get("critical_checks", [])],
        "tests": [str(path) for path in found.get("critical_tests", [])],
        "regressions": [str(path) for path in found.get("regressions", [])],
    }


def pr_tests(root: Path, changed: Sequence[str]) -> tuple[list[str], list[str]]:
    """What `pr` tests: the critical tests and those the change affects (everything when the
    impact is uncertain), and the reasons."""
    selection = impact.select(root, changed)
    if selection.full:
        return ["."], selection.reasons
    critical = profiles(root)["tests"]
    return sorted(set(critical) | selection.tests), selection.reasons


def pr(
    root: Path,
    workers: int,
    run: Callable[[Step, Path, dict[str, str]], Result] = run_step,
) -> int:
    """Before a push (#375): the quick checks, the critical checks, and the critical and
    affected tests in parallel parts, on this interpreter, in the working tree as it is. The
    full suite runs in CI on every pull request before a merge."""
    changed = impact.changed_files(root)
    wanted = set(FAST) | set(profiles(root)["checks"])
    steps = [
        Step(c.name, "", c.directory, ("bash", "-eo", "pipefail", "-c", checks.script(root, c)))
        for c in checks.load(root)
        if c.name in wanted and c.local
    ]
    tests, reasons = pr_tests(root, changed)
    print("pr: tests selected")
    print("\n".join(f"  {reason}" for reason in reasons) or "  (no change)")
    print("  " + " ".join(tests))
    variables = dict(os.environ)
    variables["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{variables.get('PATH', '')}"
    variables.pop("PYTEST_SHARD", None)
    with tempfile.TemporaryDirectory(prefix="verify-") as scratch:
        junit = Path(scratch) / "junit.xml"
        text = (
            f"{sys.executable} -m pytest -q -p tools.ci.shard -p tools.ci.timing "
            f"--junitxml={junit} " + " ".join(tests)
        )
        steps += _parted(TESTS, "", ("bash", "-c", text), junit, max(1, workers))
        with ThreadPoolExecutor(max_workers=workers + 2) as pool:
            results = list(pool.map(lambda s: run(s, root, variables), steps))
    text, status = summary(results)
    print(text)
    return status


def nightly(
    root: Path,
    workers: int,
    run: Callable[[Step, Path, dict[str, str]], Result] = run_step,
) -> int:
    """`full` with the extensive variants on (`PROPERTY_PROFILE=nightly`, as
    `property-nightly.yml` sets it): hours, never a merge gate."""
    previous = os.environ.get("PROPERTY_PROFILE")
    os.environ["PROPERTY_PROFILE"] = "nightly"
    try:
        return full(root, workers, run)
    finally:
        if previous is None:
            os.environ.pop("PROPERTY_PROFILE", None)
        else:
            os.environ["PROPERTY_PROFILE"] = previous


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.ci.verify")
    parser.add_argument("mode", choices=("fast", "pr", "full", "nightly"))
    parser.add_argument("--workers", type=int, default=max(2, os.cpu_count() or 2))
    arguments = parser.parse_args(argv)
    modes = {"fast": fast, "pr": pr, "full": full, "nightly": nightly}
    try:
        return modes[arguments.mode](REPOSITORY_ROOT, arguments.workers)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"verify {arguments.mode}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
