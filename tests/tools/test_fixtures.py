"""Shared fixtures are immutable (#376, #373 §5, Owner precision 2)."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tests.support.fixtures import freeze, frozen, private_copy, thaw
from tools.repository import REPOSITORY_ROOT

SHARED = {"class", "module", "package", "session", "unknown"}


def _template(folder: Path) -> Path:
    (folder / "nested").mkdir(parents=True)
    (folder / "a.txt").write_text("a", "utf-8")
    (folder / "nested" / "b.txt").write_text("b", "utf-8")
    return folder


# The operating system does not refuse the superuser: the sandbox runs as root, CI does not.
as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)


@as_root
def test_a_frozen_folder_refuses_every_write_until_it_is_thawed(tmp_path: Path) -> None:
    folder = _template(tmp_path / "shared")
    frozen_folder = frozen(folder)
    assert next(frozen_folder) == folder
    with pytest.raises(PermissionError):
        (folder / "a.txt").write_text("changed", "utf-8")
    with pytest.raises(PermissionError):
        (folder / "nested" / "b.txt").open("a")
    if os.name != "nt":  # Windows has no read-only folders: only its files are protected
        with pytest.raises(PermissionError):
            (folder / "new.txt").write_text("new", "utf-8")
    assert (folder / "a.txt").read_text("utf-8") == "a"
    with pytest.raises(StopIteration):
        next(frozen_folder)  # teardown thaws, so pytest can remove the folder
    (folder / "new.txt").write_text("new", "utf-8")


def test_a_private_copy_is_writable_and_leaves_the_template_unchanged(tmp_path: Path) -> None:
    template = _template(tmp_path / "shared")
    freeze(template)
    try:
        own = private_copy(template, tmp_path / "own")
        (own / "a.txt").write_text("changed", "utf-8")
        (own / "nested" / "c.txt").write_text("c", "utf-8")
        assert (template / "a.txt").read_text("utf-8") == "a"
        assert not (template / "nested" / "c.txt").exists()
    finally:
        thaw(template)


def _scope(decorator: ast.expr) -> str | None:
    """A fixture's scope; a scope that is not a literal counts as shared (fail closed)."""
    if not isinstance(decorator, ast.Call):
        return None
    for keyword in decorator.keywords:
        if keyword.arg == "scope":
            value = keyword.value
            return value.value if isinstance(value, ast.Constant) else "unknown"
    return None


def _creates_files(node: ast.AST) -> bool:
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    attributes = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    return bool(
        "tmp_path_factory" in names | attributes
        or {"mkdtemp", "TemporaryDirectory"} & (names | attributes)
    )


def _freezes(node: ast.AST) -> bool:
    """The fixture passes a folder to `frozen` and yields from it (or iterates it)."""
    return any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "frozen"
        for call in ast.walk(node)
    ) and any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node))


def _shared_fixtures_writing_files(path: Path, root: Path = REPOSITORY_ROOT) -> list[str]:
    """The shared fixtures in `path` that create a folder but do not freeze it."""
    found = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        scopes = {_scope(decorator) for decorator in node.decorator_list}
        if scopes & SHARED and _creates_files(node) and not _freezes(node):
            found.append(f"{path.relative_to(root).as_posix()}::{node.name}")
    return found


def test_every_shared_fixture_with_files_freezes_them() -> None:
    tests = [
        *REPOSITORY_ROOT.glob("tests/**/*.py"),
        *REPOSITORY_ROOT.glob("packages/*/tests/**/*.py"),
        *REPOSITORY_ROOT.glob("conformance/**/*.py"),
    ]
    unfrozen = [found for path in sorted(tests) for found in _shared_fixtures_writing_files(path)]
    assert not unfrozen, unfrozen


@pytest.mark.parametrize(
    "body",
    [
        '@pytest.fixture(scope="module")\ndef data(tmp_path_factory):\n'
        '    return tmp_path_factory.mktemp("x")\n',
        '@pytest.fixture(scope="class")\ndef data(tmp_path_factory):\n'
        '    return tmp_path_factory.mktemp("x")\n',
        "@pytest.fixture(scope=SCOPE)\ndef data():\n    return tempfile.mkdtemp()\n",
        '@pytest.fixture(scope="session")\nasync def data(tmp_path_factory):\n'
        '    return tmp_path_factory.mktemp("x")\n',
        '@pytest.fixture(scope="module")\ndef data(tmp_path_factory):\n'
        '    frozen(tmp_path_factory.mktemp("x"))\n    return 1\n',
    ],
    ids=["module", "class", "mkdtemp-unknown-scope", "async", "frozen-not-yielded"],
)
def test_the_check_finds_an_unfrozen_shared_fixture(tmp_path: Path, body: str) -> None:
    source = tmp_path / "test_example.py"
    source.write_text("import pytest\nimport tempfile\n\n" + body, "utf-8")
    assert _shared_fixtures_writing_files(source, tmp_path) == ["test_example.py::data"]


def test_the_check_accepts_a_frozen_shared_fixture(tmp_path: Path) -> None:
    source = tmp_path / "test_example.py"
    source.write_text(
        "import pytest\n\n"
        '@pytest.fixture(scope="module")\ndef data(tmp_path_factory):\n'
        '    yield from frozen(tmp_path_factory.mktemp("x"))\n',
        "utf-8",
    )
    assert _shared_fixtures_writing_files(source, tmp_path) == []
