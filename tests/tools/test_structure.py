"""#246 PR 1: file-size warnings, import cycles, façade imports and dynamic imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.checks import structure
from tools.repository import REPOSITORY_ROOT


def _package(root: Path, files: dict[str, str], exceptions: str = "") -> Path:
    for name, text in files.items():
        path = root / "packages" / "store" / "src" / "pkg" / "store" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    listed = root / structure.EXCEPTIONS.relative_to(REPOSITORY_ROOT)
    listed.parent.mkdir(parents=True, exist_ok=True)
    listed.write_text(exceptions, "utf-8")
    return root


def _codes(findings: list[structure.Finding]) -> list[tuple[str, str]]:
    return [(f.level, f.code) for f in findings]


def test_a_large_file_is_a_warning_never_a_failure(tmp_path: Path) -> None:
    root = _package(tmp_path, {"__init__.py": "", "big.py": "x = 1\n" * 801, "ok.py": "x = 1\n"})
    assert _codes(structure.sizes(root)) == [("warning", "FILE_TOO_LARGE")]
    assert structure.imports(root) == []


def test_a_justified_large_file_is_quiet_and_a_stale_entry_is_warned(tmp_path: Path) -> None:
    exceptions = (
        '[size]\n"packages/store/src/pkg/store/big.py" = "reason"\n'
        '"packages/store/src/pkg/store/ok.py" = "no longer large"\n'
    )
    root = _package(
        tmp_path, {"__init__.py": "", "big.py": "x = 1\n" * 801, "ok.py": "x = 1\n"}, exceptions
    )
    (finding,) = structure.sizes(root)
    assert finding.path.endswith("ok.py")
    assert "remove the entry" in finding.text


@pytest.mark.parametrize(
    "files",
    [
        {"a.py": "import pkg.store.b\n", "b.py": "from pkg.store import a\n"},
        {"a.py": "from . import b\n", "b.py": "from .a import thing\n"},
        {"a.py": "def f():\n    from pkg.store import b\n", "b.py": "import pkg.store.a\n"},
        {
            "a.py": "try:\n    from . import b\nexcept ImportError:\n    pass\n",
            "b.py": "from . import a\n",
        },
    ],
    ids=["absolute", "relative", "inside-a-function", "inside-try"],
)
def test_a_cycle_through_any_import_form_fails(tmp_path: Path, files: dict[str, str]) -> None:
    root = _package(tmp_path, {"__init__.py": "", **files})
    (finding,) = structure.imports(root)
    assert (finding.level, finding.code, finding.text) == (
        "error",
        "IMPORT_CYCLE",
        "pkg.store.a -> pkg.store.b",
    )


def test_a_cycle_through_typing_only_imports_is_not_reported(tmp_path: Path) -> None:
    typing = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import a\n"
    root = _package(tmp_path, {"__init__.py": "", "a.py": "from . import b\n", "b.py": typing})
    assert structure.imports(root) == []


def test_an_internal_module_importing_the_facade_fails(tmp_path: Path) -> None:
    root = _package(tmp_path, {"__init__.py": "", "lake.py": "", "x.py": "from . import lake\n"})
    assert _codes(structure.imports(root)) == [("error", "FACADE_IMPORTED")]


def test_a_listed_cycle_or_facade_import_passes_and_a_stale_one_fails(tmp_path: Path) -> None:
    exceptions = (
        '[cycle]\n"pkg.store.a -> pkg.store.b" = "split later"\n'
        '"pkg.store.gone -> pkg.store.old" = "already split"\n'
        '[facade]\n"pkg.store.x" = "split later"\n"pkg.store.y" = "already split"\n'
    )
    files = {
        "__init__.py": "",
        "lake.py": "",
        "a.py": "from . import b\n",
        "b.py": "from . import a\n",
        "x.py": "from . import lake\n",
    }
    found = structure.imports(_package(tmp_path, files, exceptions))
    assert sorted((f.code, f.path) for f in found) == [
        ("FACADE_IMPORTED", "pkg.store.y"),
        ("IMPORT_CYCLE", "pkg.store.gone -> pkg.store.old"),
    ]


def test_a_dynamic_import_is_listed(tmp_path: Path) -> None:
    root = _package(
        tmp_path, {"__init__.py": "", "a.py": "import importlib\nimportlib.import_module(x)\n"}
    )
    assert _codes(structure.dynamic(root)) == [("note", "DYNAMIC_IMPORT")]


def test_the_repository_has_no_unlisted_cycle_or_facade_import() -> None:
    assert structure.imports(REPOSITORY_ROOT) == []
