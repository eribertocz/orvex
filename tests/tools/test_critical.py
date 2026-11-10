"""#427: a critical test never passes in CI by not running; any other test still may skip."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools.ci import critical
from tools.repository import REPOSITORY_ROOT

BODY = """import pytest
def test_skips() -> None:
    pytest.skip("no service here")
@pytest.mark.xfail(reason="#1")
def test_expects_to_fail() -> None:
    raise AssertionError
@pytest.mark.xfail(reason="#2", strict=False)
def test_passes_while_marked_to_fail() -> None:
    pass
"""
MODULE_SKIP = """import pytest
pytest.skip("no service here", allow_module_level=True)
def test_never_collected() -> None:
    pass
"""
IMPORT_SKIP = """import pytest
pytest.importorskip("a_module_that_is_not_installed")
def test_never_collected() -> None:
    pass
"""


def run_in(folder: str, ci: bool, report: Path, body: str = BODY) -> tuple[int, str]:
    """Run planted tests placed in `folder` of the repository; (exit status, summary)."""
    path = REPOSITORY_ROOT / folder / f"test_tmp_{uuid.uuid4().hex}.py"
    path.write_text(body, "utf-8")
    try:
        env = {k: v for k, v in os.environ.items() if k != "CI"} | ({"CI": "true"} if ci else {})
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:randomly",
                str(path),
                f"--junitxml={report}",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPOSITORY_ROOT,
            check=False,
        )
    finally:
        path.unlink()
    return done.returncode, done.stdout.strip().splitlines()[-1]


def test_a_critical_test_that_skips_xfails_or_xpasses_fails_in_ci(tmp_path: Path) -> None:
    status, summary = run_in("tests/properties", ci=True, report=tmp_path / "j.xml")
    assert status == 1
    assert "3 failed" in summary


@pytest.mark.parametrize("body", [MODULE_SKIP, IMPORT_SKIP], ids=["module-level", "importorskip"])
def test_a_critical_module_skipped_while_collecting_fails_in_ci(tmp_path: Path, body: str) -> None:
    status, summary = run_in("tests/properties", ci=True, report=tmp_path / "j.xml", body=body)
    assert status != 0
    assert "error" in summary
    # Exit 5: pytest collected no test, which is what a skipped module is everywhere else.
    assert run_in("tests/properties", ci=False, report=tmp_path / "k.xml", body=body)[0] == 5
    assert run_in("tests/tools", ci=True, report=tmp_path / "m.xml", body=body)[0] == 5


@pytest.mark.parametrize("body", [MODULE_SKIP, IMPORT_SKIP], ids=["module-level", "importorskip"])
def test_the_junit_check_finds_a_critical_module_skipped_while_collecting(
    tmp_path: Path, body: str
) -> None:
    run_in("tests/properties", ci=False, report=tmp_path / "j.xml", body=body)
    assert len(critical.skipped(tmp_path / "j.xml", critical.critical_paths())) == 1


def test_outside_ci_or_outside_the_critical_set_a_skip_stays_a_skip(tmp_path: Path) -> None:
    assert run_in("tests/properties", ci=False, report=tmp_path / "a.xml")[0] == 0
    assert run_in("tests/tools", ci=True, report=tmp_path / "b.xml")[0] == 0


def test_the_junit_check_finds_every_critical_case_not_run(tmp_path: Path) -> None:
    run_in("tests/properties", ci=False, report=tmp_path / "j.xml")
    found = critical.skipped(tmp_path / "j.xml", critical.critical_paths())
    assert len(found) == 2
    assert critical.main([str(tmp_path / "j.xml")]) == 1
    run_in("tests/tools", ci=False, report=tmp_path / "k.xml")
    assert critical.skipped(tmp_path / "k.xml", critical.critical_paths()) == []


def test_the_critical_set_includes_conformance_and_properties() -> None:
    paths = critical.critical_paths()
    for folder in critical.ALWAYS:
        assert critical.is_critical(f"{folder}/test_x.py", paths)
    assert not critical.is_critical("tests/tools/test_x.py", paths)
