"""The impact map (#375, #373 §3): conservative, explained, and true to the real imports."""

from __future__ import annotations

import ast
import subprocess
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


def test_a_contract_change_selects_the_full_suite() -> None:
    # #373 §3: contracts are read across modules, tests and conformance.
    assert impact.select(ROOT, ["contracts/model-config/v1/README.md"]).full


def test_a_rename_lists_both_paths(tmp_path: Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "old.py").write_text("x = 1\n" * 20)
    git("add", ".")
    git("commit", "-q", "-m", "a")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("mv", "old.py", "new.py")
    git("commit", "-q", "-m", "b")
    assert impact.changed_files(tmp_path) == ["new.py", "old.py"]


def repository_reads(path: Path) -> set[str]:
    """The repository paths a test file depends on beyond its package imports: the `tools.*`
    modules it imports and the files it reads as `<…ROOT> / "literal"`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        names = (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        for name in names:
            parts = name.split(".")
            if parts[0] == "tools" and len(parts) > 1 and parts[1] != "repository":
                found.add("/".join(parts[:2]) + "/__init__.py")
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id.endswith("ROOT")
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
        ):
            found |= samples(node.right.value)
    return found


def samples(target: str) -> set[str]:
    """A file read stands for itself; a folder read, for one tracked file under each of its
    entries, so a folder spanning several packages is checked against each of them."""
    if not (ROOT / target).is_dir():
        return {target}
    tracked = subprocess.run(
        ["git", "ls-files", "--", target], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    first: dict[str, str] = {}
    for path in sorted(tracked):
        entry = Path(path).relative_to(target).parts[0]
        first.setdefault(entry, path)
    return set(first.values())


def test_every_file_a_test_reads_selects_that_test() -> None:
    # Owner precision 1: a test that reads a file outside its package (a lock, a contract, a
    # corpus case, a checker) is selected when that file changes.
    tests = sorted(ROOT.glob("tests/**/test_*.py")) + sorted(ROOT.glob("packages/*/tests/**/*.py"))
    missing = []
    for test in tests:
        relative = test.relative_to(ROOT).as_posix()
        if test == Path(__file__):
            continue  # this file reads the whole layout; a new package also changes the locks
        for read in sorted(repository_reads(test)):
            selection = impact.select(ROOT, [read])
            if not selection.full and not any(
                relative.startswith(f"{p}/") for p in selection.paths()
            ):
                missing.append(f"{relative} reads {read}")
    assert not missing, missing


def test_every_change_gets_a_reason() -> None:
    changed = ["docs/adr/README.md", "packages/semantic/src/x.py", "web/src/a.ts"]
    selection = impact.select(ROOT, changed)
    for path in changed:
        assert any(reason.startswith(path) for reason in selection.reasons)


def tool_references(path: Path) -> set[str]:
    """The `tools.*` modules a test file reaches: imported, or named in a string (a subprocess's
    `-m tools.x`), each that exists as a file of the repository."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            parts = name.split(".")
            if parts[0] != "tools" or len(parts) < 2 or not all(p.isidentifier() for p in parts):
                continue
            for n in range(len(parts), 1, -1):
                candidate = Path(*parts[:n]).with_suffix(".py")
                if (REPOSITORY_ROOT / candidate).is_file():
                    found.add(candidate.as_posix())
                    break
    return found


def covers(selection: impact.Selection, test: str) -> bool:
    return selection.full or any(
        test == t or test.startswith(t.rstrip("/") + "/") for t in selection.tests
    )


def test_every_test_that_reaches_a_tool_is_selected_when_that_tool_changes() -> None:
    # #427: a dependency through a subprocess or a string is invisible to the package graph;
    # the selection must still run every test that reaches the changed tool.
    missed = []
    for test in sorted(REPOSITORY_ROOT.glob("packages/*/tests/**/test_*.py")):
        relative = test.relative_to(REPOSITORY_ROOT).as_posix()
        for tool in sorted(tool_references(test)):
            if not covers(impact.select(REPOSITORY_ROOT, [tool]), relative):
                missed.append(f"{relative} reaches {tool}")
    assert missed == []


def test_a_test_reaching_a_tool_through_a_subprocess_is_found() -> None:
    synthetic = REPOSITORY_ROOT / "packages/store/tests/test_sources.py"
    assert "tools/synthetic/generate.py" in tool_references(synthetic)
    narrowed = impact.Selection(tests={"tests/tools"})
    assert not covers(narrowed, "packages/store/tests/test_sources.py")  # the old map missed it
