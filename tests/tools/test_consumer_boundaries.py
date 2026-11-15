"""Consumer boundaries (#486 G2), in report mode until step 7: the planted imports are caught."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tools.checks.boundaries import run
from tools.checks.consumers import AREAS, consumer_findings, consumer_scan, consumer_summary
from tools.repository import REPOSITORY_ROOT, identity

from .test_checks import make_repository

NAMESPACE = str(identity(REPOSITORY_ROOT)["python_namespace"])
ASSEMBLY = "packages/server/src/orvex/server/assembly.py"

PLANTED = {
    "server": ("packages/server/src/orvex/server/x.py", "from orvex.store.lake import open_lake\n"),
    "server test": ("packages/server/tests/test_x.py", "import orvex.access.errors\n"),
    "runner": ("conformance/runners/x.py", "from orvex.semantic import errors\n"),
    "tool": ("tools/x.py", "from orvex.transform.incremental import plan\n"),
    "repository test": ("tests/x/test_y.py", "import orvex.semantic.evidence as e\n"),
    "dynamic": (
        "tests/x/test_z.py",
        'import importlib\nimportlib.import_module("orvex.store.lake")\n',
    ),
    "dunder": ("tools/y.py", '__import__("orvex.mcp.server")\n'),
    "api outside assembly": ("packages/server/src/orvex/server/y.py", "import orvex.store.api\n"),
    "internal in assembly": (ASSEMBLY, "from orvex.store.lake import x\n"),
    "test helpers in a runner": ("conformance/runners/y.py", "import orvex.testing.environment\n"),
    "test helpers in a tool": ("tools/z.py", "from orvex.testing.fixtures import frozen\n"),
    "a server internal beside the assembly": (
        "tests/x/test_w.py",
        "from orvex.server.assembly import provider\nfrom orvex.server import cli\n",
    ),
}


def findings(relative: str, source: str) -> list[str]:
    return [f.code for f in consumer_findings(ast.parse(source), NAMESPACE, relative)]


@pytest.mark.parametrize(("relative", "source"), PLANTED.values(), ids=PLANTED.keys())
def test_a_planted_internal_import_is_caught(relative: str, source: str) -> None:
    assert findings(relative, source) == ["CONSUMER_BOUNDARY_VIOLATION"]


ALLOWED = {
    "contracts": ("tools/x.py", "from orvex.contracts.store.v1 import StoreError\n"),
    "contracts package": (
        "tests/test_x.py",
        f"from {NAMESPACE} import contracts\nimport {NAMESPACE}\n",
    ),
    "the server's own modules": (
        "packages/server/src/orvex/server/x.py",
        "import orvex.server.cli\n",
    ),
    "server tests of the server": ("packages/server/tests/t.py", "from orvex.server import cli\n"),
    "api in assembly": (ASSEMBLY, "import orvex.store.api\nfrom orvex.semantic.api import X\n"),
    "relative": ("packages/server/src/orvex/server/x.py", "from . import cli\n"),
    "third party": ("tools/x.py", "import duckdb\nfrom tools.repository import identity\n"),
    "computed dynamic import": ("tools/x.py", "import importlib\nimportlib.import_module(name)\n"),
    "test helpers in a repository test": (
        "tests/x/t.py",
        "from orvex.testing.bounds import linear\n",
    ),
    "test helpers in a server test": (
        "packages/server/tests/t.py",
        "from orvex.testing import azurite\n",
    ),
    "the composition root from a test": (
        "tests/x/t.py",
        "from orvex.server.assembly import provider\n",
    ),
    "the composition root from a runner": (
        "conformance/runners/x.py",
        "import orvex.server.assembly\n",
    ),
    "the composition root from a tool": ("tools/x.py", "from orvex.server import assembly\n"),
}


@pytest.mark.parametrize(("relative", "source"), ALLOWED.values(), ids=ALLOWED.keys())
def test_contracts_own_code_and_api_in_assembly_are_allowed(relative: str, source: str) -> None:
    assert findings(relative, source) == []


def test_a_tests_import_of_the_server_from_outside_it_is_caught() -> None:
    assert findings("tests/x.py", "from orvex.server.cli import main\n") == [
        "CONSUMER_BOUNDARY_VIOLATION"
    ]


def test_the_scan_covers_the_consumers_and_leaves_the_modules_to_the_module_rule(
    tmp_path: Path,
) -> None:
    files = dict(PLANTED.values())
    files["packages/store/src/orvex/store/x.py"] = "import orvex.store.lake\n"
    files["conformance/semantic-corpus/x.py"] = "import orvex.store.lake\n"
    found = consumer_scan(make_repository(tmp_path, files))
    assert sorted({f.path for f in found}) == sorted({relative for relative, _ in PLANTED.values()})
    summary = consumer_summary(found)
    assert summary.startswith(f"consumer boundaries (report mode until #486 step 7): {len(found)}")
    assert "server 4" in summary
    assert "runners 2" in summary
    assert "tools 3" in summary
    assert "tests 3" in summary


def test_report_mode_prints_every_violation_and_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    (tmp_path / "r").mkdir()
    repository = make_repository(tmp_path / "r", dict(PLANTED.values()))
    assert run(repository, empty, empty) == 0
    out = capsys.readouterr().out
    for relative, _ in PLANTED.values():
        assert f"CONSUMER_BOUNDARY_VIOLATION: {relative}:" in out
    assert "consumer boundaries (report mode until #486 step 7)" in out


def test_server_code_importing_the_test_helpers_fails_as_a_module_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The server's code is product code too: the blocking module rule catches it, not only the
    # consumer report.
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    files = {"packages/server/src/orvex/server/z.py": "from orvex.testing import bounds\n"}
    (tmp_path / "r").mkdir()
    assert run(make_repository(tmp_path / "r", files), empty, empty) == 1
    assert "MODULE_BOUNDARY_VIOLATION: packages/server/src/orvex/server/z.py:1" in (
        capsys.readouterr().out
    )


def test_a_module_boundary_violation_still_fails(tmp_path: Path) -> None:
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    files = {"packages/store/src/orvex/store/x.py": "import orvex.access.errors\n"}
    (tmp_path / "r").mkdir()
    assert run(make_repository(tmp_path / "r", files), empty, empty) == 1


# The files per area that still use module internals, measured on 2026-11-15 (#486 §1 counts
# 48 `server` source files; the `server` area here also holds its 32 test files). Report mode
# never lets a count grow, and each later step of #486 lowers it, to zero at step 7.
CEILINGS = {"server": 80, "runners": 10, "tools": 7, "tests": 66}


def test_the_repository_report_sees_the_coupling_and_never_more() -> None:
    found = consumer_scan(REPOSITORY_ROOT)
    files: dict[str, set[str]] = {area: set() for area in CEILINGS}
    for finding in found:
        area = next(a for a, start in AREAS.items() if finding.path.startswith(start))
        files[area].add(finding.path)
    for area, ceiling in CEILINGS.items():
        assert 0 < len(files[area]) <= ceiling, (area, len(files[area]))
    sources = {path for path in files["server"] if path.startswith("packages/server/src/")}
    assert 0 < len(sources) <= 48
