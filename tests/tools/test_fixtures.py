"""Shared fixtures are immutable (#376, #373 §5, Owner precision 2)."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tests.support.fixtures import freeze, frozen, private_copy, thaw
from tools.repository import REPOSITORY_ROOT

SHARED = {"module", "package", "session"}


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


def _shared_fixtures_writing_files(path: Path, root: Path = REPOSITORY_ROOT) -> list[str]:
    """The shared fixtures in `path` that create a folder but do not freeze it."""
    found = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if not isinstance(node, ast.FunctionDef):
            continue
        scopes = {
            keyword.value.value
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            for keyword in decorator.keywords
            if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant)
        }
        if not scopes & SHARED:
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if "tmp_path_factory" in names and "frozen" not in names:
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


def test_the_check_finds_an_unfrozen_shared_fixture(tmp_path: Path) -> None:
    source = tmp_path / "test_example.py"
    source.write_text(
        "import pytest\n\n"
        '@pytest.fixture(scope="module")\n'
        "def data(tmp_path_factory):\n"
        '    return tmp_path_factory.mktemp("x")\n',
        "utf-8",
    )
    assert _shared_fixtures_writing_files(source, tmp_path) == ["test_example.py::data"]
