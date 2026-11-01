"""The local verification (tools/ci/verify.py, #240): it runs what the canonical list marks
local, reports what it does not run with the reason, never counts a test that did not run as
passed, and fails on any failure."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.ci import checks, verify
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
    assert "diff /tmp/first.txt /tmp/second.txt" in text


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
