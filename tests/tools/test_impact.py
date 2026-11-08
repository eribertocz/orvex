"""The impact map (#375, #373 §3): conservative, explained, and true to the real imports."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from tools.ci import impact
from tools.repository import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def imported_packages(folder: Path, seen: set[Path] | None = None) -> set[str]:
    """The package folders a test folder's code imports, following the repository's own shared
    modules (`tests.*`, `conformance.*`) it imports too."""
    seen = set() if seen is None else seen
    found: set[str] = set()
    names = {p.name for p in (ROOT / "packages").iterdir() if (p / "pyproject.toml").is_file()}
    for path in sorted(folder.rglob("*.py")) if folder.is_dir() else [folder]:
        if path in seen:
            continue
        seen.add(path)
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                parts = module.split(".")
                if len(parts) > 1 and parts[1] in names and parts[0] not in ("tests", "tools"):
                    found.add(parts[1])
                elif parts[0] in ("tests", "conformance") and len(parts) > 1:
                    target = ROOT.joinpath(*parts)
                    for candidate in (target.with_suffix(".py"), target):
                        if candidate.exists():
                            found |= imported_packages(candidate, seen)
    return found


def test_every_test_folder_declares_the_packages_it_imports() -> None:
    declared = tomllib.loads(impact.MAP.read_text("utf-8"))["tests"]
    graph = impact.packages(ROOT)
    for folder, packages in declared.items():
        # A change to any package the folder imports must reach a package it declares (itself
        # or through the packages that depend on it), or the folder would not be selected.
        missing = {
            imported
            for imported in imported_packages(ROOT / folder)
            if not impact.dependents(graph, [imported]) & set(packages)
        }
        assert not missing, f"{folder} imports {sorted(missing)} but declares {packages}"


def test_every_repository_test_folder_is_mapped() -> None:
    declared = set(tomllib.loads(impact.MAP.read_text("utf-8"))["tests"])
    folders = {
        f"tests/{p.name}"
        for p in (ROOT / "tests").iterdir()
        if p.is_dir() and any(p.glob("test_*.py"))
    }
    assert folders <= declared


def test_package_dependencies_are_the_pyproject_ones() -> None:
    graph = impact.packages(ROOT)
    assert graph["server"] >= {"store", "semantic", "transform", "access", "contracts"}
    assert graph["store"] == {"contracts"}


@pytest.mark.parametrize(
    "path",
    [
        "somewhere/new.txt",
        "tools/ci/verify.py",
        "tests/support/policies.py",
        "conformance/runners/corpus_execution.py",
        "pyproject.toml",
        ".github/workflows/python.yml",
        "packages/contracts/src/x.py",
        "contracts/unknown-contract/v1/schema.json",
    ],
)
def test_an_unknown_or_global_path_selects_the_full_suite(path: str) -> None:
    selection = impact.select(ROOT, [path])
    assert selection.full
    assert selection.paths() == ["."]
    assert any(path in reason for reason in selection.reasons)


def test_a_package_change_selects_it_and_everything_that_depends_on_it() -> None:
    selection = impact.select(ROOT, ["packages/store/src/x/lake.py"])
    assert not selection.full
    assert {"packages/store/tests", "packages/server/tests", "tests/conformance"} <= set(
        selection.paths()
    )
    assert "packages/semantic/tests" not in selection.paths()  # semantic does not use store


@pytest.mark.parametrize(
    "path",
    [
        "packages/transform/tests/fixtures/model.yaml",  # a fixture
        "packages/transform/src/x/models/staging.sql",  # SQL
        "packages/transform/src/x/schema.json",  # a schema
    ],
)
def test_declarative_files_of_a_package_select_it(path: str) -> None:
    selection = impact.select(ROOT, [path])
    assert "packages/transform/tests" in selection.paths()
    assert "packages/server/tests" in selection.paths()  # server depends on transform


def test_a_contract_change_selects_its_users_and_the_contract_tests() -> None:
    selection = impact.select(ROOT, ["contracts/model-config/v1/README.md"])
    paths = set(selection.paths())
    assert {"tests/contracts", "packages/transform/tests", "packages/server/tests"} <= paths
    assert any("used by transform" in reason for reason in selection.reasons)


def test_every_change_gets_a_reason() -> None:
    changed = ["docs/adr/README.md", "packages/semantic/src/x.py", "web/src/a.ts"]
    selection = impact.select(ROOT, changed)
    for path in changed:
        assert any(reason.startswith(path) for reason in selection.reasons)
