"""The local verification (tools/ci/verify.py, #240): it runs what the canonical list marks
local, reports what it does not run with the reason, never counts a test that did not run as
passed, and fails on any failure."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from tools.ci import checks, trends, verify
from tools.repository import REPOSITORY_ROOT


def test_full_plans_every_local_check_and_reports_the_rest_as_not_run(tmp_path: Path) -> None:
    steps, not_run = verify.plan(REPOSITORY_ROOT, ("3.15", "3.14"), tmp_path)
    listed = checks.load(REPOSITORY_ROOT)
    planned = {s.name for s in steps}
    for check in listed:
        if check.name == verify.TESTS:
            assert {f"{verify.TESTS}-{p}" for p in check.pythons} <= planned
        elif check.local:
            assert check.name in planned
        else:
            assert any(r.name == check.name and r.detail == check.reason for r in not_run)
    scoped = {job for job, _ in checks.not_run_jobs(REPOSITORY_ROOT)}
    assert scoped
    assert scoped <= {r.name for r in not_run}
    assert all(r.status == "not run" and r.detail for r in not_run)


def test_the_tests_run_on_every_python_of_the_matrix_with_their_own_report(tmp_path: Path) -> None:
    steps, _ = verify.plan(REPOSITORY_ROOT, ("3.15", "3.14"), tmp_path)
    tests = [s for s in steps if s.junit]
    assert [s.python for s in tests] == ["3.15", "3.14"]
    assert len({s.junit for s in tests}) == 2
    assert all(f"--junitxml={s.junit}" in s.command[-1] for s in tests)


def test_a_check_runs_the_workflow_step_itself() -> None:
    lock = next(c for c in checks.load(REPOSITORY_ROOT) if c.name == "python-lock")
    text = checks.script(REPOSITORY_ROOT, lock)
    assert text.startswith(lock.run)
    assert 'diff "/tmp/$python-first.txt" "/tmp/$python-second.txt"' in text


JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite>
  <testcase name="a"/>
  <testcase name="b"><skipped message="no PostgreSQL catalog configured for this run"/></testcase>
  <testcase name="c"><skipped message="no PostgreSQL catalog configured for this run"/></testcase>
  <testcase name="d"><skipped message="no Azurite"/></testcase>
</testsuite></testsuites>
"""


def test_skipped_tests_are_counted_by_reason_and_not_as_passed(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT)
    total, reasons = verify.skipped_tests(junit)
    assert total == 4
    assert reasons == {"no PostgreSQL catalog configured for this run": 2, "no Azurite": 1}
    result = verify.Result("python-tests-3.15", "passed", tests=total, skipped=reasons)
    text, status = verify.summary([result])
    assert status == 0
    assert "1 of 4 tests ran" in text
    assert "2  no PostgreSQL catalog configured for this run" in text


def test_any_failure_makes_the_exit_status_non_zero() -> None:
    text, status = verify.summary(
        [
            verify.Result("ruff-check", "passed"),
            verify.Result("mypy-tests", "failed", "error: x"),
            verify.Result("web-check", "not run", "needs Node 24"),
        ]
    )
    assert status == 1
    assert "VERIFY FAILED" in text
    assert "web-check: needs Node 24" in text
    assert "| error: x" in text


def test_only_not_run_items_do_not_pass_silently() -> None:
    text, status = verify.summary([verify.Result("web-check", "not run", "needs Node 24")])
    assert status == 0
    assert "NOT RUN (1)" in text
    assert "not passes" in text


def test_a_step_that_fails_is_reported_with_its_output(tmp_path: Path) -> None:
    step = verify.Step("x", "", None, ("bash", "-c", "echo boom; exit 3"))
    result = verify.run_step(step, tmp_path, {"PATH": "/usr/bin:/bin"})
    assert result.status == "failed"
    assert "boom" in result.detail


def test_a_test_run_without_a_report_fails(tmp_path: Path) -> None:
    step = verify.Step("t", "", None, ("bash", "-c", "true"), str(tmp_path / "none.xml"))
    assert verify.run_step(step, tmp_path, {"PATH": "/usr/bin:/bin"}).status == "failed"


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["packages/store/src/orvex/store/lake.py"], ["packages/store/tests"]),
        (
            ["tools/ci/verify.py", "packages/transform/tests/x.py"],
            ["packages/transform/tests", "tests"],
        ),
        (["docs/process/charter.md"], []),
    ],
)
def test_fast_runs_the_tests_of_the_changed_modules(files: list[str], expected: list[str]) -> None:
    assert verify.changed_tests(REPOSITORY_ROOT, files) == expected


def test_full_refuses_uncommitted_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "--quiet", tmp_path], check=True)
    (tmp_path / "new.txt").write_text("x")
    assert verify.full(tmp_path, 1) == 2
    assert "new.txt" in capsys.readouterr().out


def test_the_test_step_must_still_write_its_report_where_verify_reads_it() -> None:
    assert verify._with_report("pytest --junitxml=junit.xml\nmore", "/r.xml").startswith(
        "pytest --junitxml=/r.xml"
    )
    with pytest.raises(RuntimeError, match="junitxml"):
        verify._with_report("pytest --junitxml=other.xml", "/r.xml")


def test_the_planned_test_command_is_the_whole_workflow_step(tmp_path: Path) -> None:
    steps, _ = verify.plan(REPOSITORY_ROOT, ("3.15", "3.14"), tmp_path)
    check = next(c for c in checks.load(REPOSITORY_ROOT) if c.name == verify.TESTS)
    step = next(s for s in steps if s.junit)
    assert step.command[-1] == verify._with_report(
        checks.script(REPOSITORY_ROOT, check), step.junit or ""
    )


# --- profiles (#375) ----------------------------------------------------------------------------


def test_the_critical_sets_name_real_tests_and_local_checks() -> None:
    critical = verify.profiles(REPOSITORY_ROOT)
    assert critical["tests"]
    assert all((REPOSITORY_ROOT / path).exists() for path in critical["tests"])
    local = {c.name for c in checks.load(REPOSITORY_ROOT) if c.local}
    assert set(critical["checks"]) <= local


def test_every_nightly_counterexample_is_a_pr_and_full_regression_test() -> None:
    # #377, #373 §6: a counterexample a nightly run found is committed (`@example`) in a file
    # listed as a regression, and every regression is in a critical set, so `pr` and `full` run
    # it, never only nightly.
    critical = verify.profiles(REPOSITORY_ROOT)
    regressions = critical["regressions"]
    assert regressions
    for path in regressions:
        assert (REPOSITORY_ROOT / path).is_file(), path
        assert any(path == c or path.startswith(c.rstrip("/") + "/") for c in critical["tests"])
    committed = subprocess.run(
        ["git", "grep", "-l", "-E", r"^\s*@example\(", "--", "*test_*.py"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(committed) == sorted(regressions)


def test_pr_always_runs_the_critical_tests_and_everything_when_unsure() -> None:
    critical = set(verify.profiles(REPOSITORY_ROOT)["tests"])
    tests, reasons = verify.pr_tests(REPOSITORY_ROOT, ["docs/roadmap.md"])
    assert critical <= set(tests)
    assert reasons
    assert verify.pr_tests(REPOSITORY_ROOT, ["unknown/file.txt"])[0] == ["."]
    assert verify.pr_tests(REPOSITORY_ROOT, [])[0] == sorted(critical)


def test_the_full_suite_runs_in_disjoint_parts_with_their_own_reports(tmp_path: Path) -> None:
    steps, _ = verify.plan(REPOSITORY_ROOT, ("3.15", "3.14"), tmp_path, total=3)
    for python in ("3.15", "3.14"):
        parts = [s for s in steps if s.junit and s.python == python]
        assert [dict(s.variables)["PYTEST_SHARD"] for s in parts] == ["0/3", "1/3", "2/3"]
        assert len({s.junit for s in parts}) == 3
        assert all(f"--junitxml={s.junit}" in s.command[-1] for s in parts)


def test_parts_are_sized_by_the_workers_shared_by_the_pythons() -> None:
    assert verify.parts(2, 2) == 1
    assert verify.parts(8, 2) == 4
    assert verify.parts(1, 2) == 1


def test_every_test_file_belongs_to_full(tmp_path: Path) -> None:
    # #373 §2: no test is in no profile. pytest collects its `testpaths` whole (nothing ignores
    # or deselects a path) and `governance-scripts` discovers the workflow scripts' tests; `full`
    # runs both.
    configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text("utf-8"))
    options = configuration["tool"]["pytest"]["ini_options"]
    assert not {"norecursedirs", "collect_ignore"} & set(options)
    assert not any(o.startswith(("--ignore", "--deselect", "-k", "-m")) for o in options["addopts"])
    roots = [f"{p}/" for p in options["testpaths"]]
    scripts = next(c for c in checks.load(REPOSITORY_ROOT) if c.name == "governance-scripts")
    assert scripts.local
    assert "discover -p 'test_*.py'" in scripts.run
    assert scripts.directory == ".github/workflows/scripts"
    # The same list the trend report shows on every main build (#377).
    assert trends.unprofiled(REPOSITORY_ROOT) == []
    steps, _ = verify.plan(REPOSITORY_ROOT, ("3.15", "3.14"), tmp_path, total=3)
    names = {s.name for s in steps}
    assert "governance-scripts" in names
    for step in steps:
        if step.junit:  # the test parts run the configured suite, with no paths of their own
            assert "pytest" in step.command[-1]
            assert not any(r.rstrip("/") in step.command[-1].split() for r in roots)


def test_nightly_is_full_with_the_extensive_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def fake_full(root: Path, workers: int, run: object = None) -> int:
        seen.append(os.environ.get("PROPERTY_PROFILE"))
        return 0

    monkeypatch.delenv("PROPERTY_PROFILE", raising=False)
    monkeypatch.setattr(verify, "full", fake_full)
    assert verify.main(["nightly", "--workers", "2"]) == 0
    assert seen == ["nightly"]
    assert "PROPERTY_PROFILE" not in os.environ


def test_every_reader_of_the_report_reads_the_planned_one(tmp_path: Path) -> None:
    """The step's later commands (`durations emit`, `critical`) read the report the run writes,
    never a `junit.xml` that does not exist in the checkout (#440)."""
    for total in (1, 3):
        steps, _ = verify.plan(REPOSITORY_ROOT, ("3.14",), tmp_path, total=total)
        for step in [s for s in steps if s.junit]:
            text = step.command[-1]
            assert re.search(r"(?<![\w/.-])junit\.xml\b", text) is None, text
            assert text.count(str(step.junit)) >= 3  # written, emitted, checked
    assert verify._with_report("a --junitxml=junit.xml\nb junit.xml\nc junit-x.xml", "/r.xml") == (
        "a --junitxml=/r.xml\nb /r.xml\nc junit-x.xml"
    )


def test_a_failed_test_run_is_never_hidden_by_the_commands_after_it(tmp_path: Path) -> None:
    """The test step runs as GitHub runs it, stopping at the first failing command: pytest
    failing then `critical` passing is a failed step (#440)."""
    for steps in (
        verify.plan(REPOSITORY_ROOT, ("3.14",), tmp_path)[0],
        verify.plan(REPOSITORY_ROOT, ("3.14",), tmp_path, total=3)[0],
    ):
        for step in [s for s in steps if s.junit]:
            assert step.command[:3] == ("bash", "-eo", "pipefail")
    script = "false\ntrue"
    assert subprocess.run([*verify._SHELL, script], check=False).returncode != 0
