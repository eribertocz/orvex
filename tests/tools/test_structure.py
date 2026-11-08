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


def test_a_large_file_fails_unless_listed_with_its_ceiling(tmp_path: Path) -> None:
    root = _package(tmp_path, {"__init__.py": "", "big.py": "x = 1\n" * 801, "ok.py": "x = 1\n"})
    assert _codes(structure.sizes(root)) == [("error", "FILE_TOO_LARGE")]
    assert structure.imports(root) == []


def test_a_listed_file_may_not_grow_past_its_ceiling(tmp_path: Path) -> None:
    exceptions = '[size]\n"packages/store/src/pkg/store/big.py" = { most = 801, reason = "#1" }\n'
    files = {"__init__.py": "", "big.py": "x = 1\n" * 801}
    assert structure.sizes(_package(tmp_path / "a", files, exceptions)) == []
    grown = _package(tmp_path / "b", {**files, "big.py": "x = 1\n" * 802}, exceptions)
    (finding,) = structure.sizes(grown)
    assert (finding.level, finding.code) == ("error", "FILE_GREW")


def test_a_shrunk_file_is_warned_to_lower_its_ceiling(tmp_path: Path) -> None:
    exceptions = '[size]\n"packages/store/src/pkg/store/big.py" = { most = 900, reason = "#1" }\n'
    root = _package(tmp_path, {"__init__.py": "", "big.py": "x = 1\n" * 850}, exceptions)
    (finding,) = structure.sizes(root)
    assert (finding.level, "lower `most`" in finding.text) == ("warning", True)


def test_an_entry_without_a_ceiling_fails(tmp_path: Path) -> None:
    exceptions = '[size]\n"packages/store/src/pkg/store/big.py" = "reason only"\n'
    root = _package(tmp_path, {"__init__.py": "", "big.py": "x = 1\n" * 801}, exceptions)
    (finding,) = structure.sizes(root)
    assert (finding.level, finding.code) == ("error", "FILE_TOO_LARGE")


def test_a_stale_entry_is_warned(tmp_path: Path) -> None:
    exceptions = '[size]\n"packages/store/src/pkg/store/ok.py" = { most = 900, reason = "#1" }\n'
    root = _package(tmp_path, {"__init__.py": "", "ok.py": "x = 1\n"}, exceptions)
    (finding,) = structure.sizes(root)
    assert finding.path.endswith("ok.py")
    assert "remove the entry" in finding.text


def test_generated_contract_modules_are_not_checked() -> None:
    assert structure.generated("packages/contracts/src/orvex/contracts/semantic_error/v1.py")
    assert not structure.generated("packages/contracts/src/orvex/contracts/__init__.py")
    assert not structure.generated("packages/store/src/orvex/store/v1.py")


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


def test_a_cycle_through_a_package_init_run_before_its_submodule_fails(tmp_path: Path) -> None:
    files = {
        "__init__.py": "",
        "a.py": "from pkg.store.sub.x import g\n",
        "sub/__init__.py": "from pkg.store.a import f\n",
        "sub/x.py": "g = 1\n",
    }
    (finding,) = structure.imports(_package(tmp_path, files))
    assert (finding.code, finding.text) == ("IMPORT_CYCLE", "pkg.store.a -> pkg.store.sub")


def test_a_package_init_importing_its_own_submodule_is_not_a_cycle(tmp_path: Path) -> None:
    files = {"__init__.py": "from .a import f\n", "a.py": "from pkg.store.b import g\n", "b.py": ""}
    assert structure.imports(_package(tmp_path, files)) == []


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


@pytest.mark.parametrize(
    "text",
    [
        "import importlib\nimportlib.import_module(x)\n",
        "from importlib import import_module\nimport_module(x)\n",
        "__import__(x)\n",
    ],
    ids=["attribute", "bare-name", "builtin"],
)
def test_a_dynamic_import_is_listed(tmp_path: Path, text: str) -> None:
    root = _package(tmp_path, {"__init__.py": "", "a.py": text})
    assert _codes(structure.dynamic(root)) == [("note", "DYNAMIC_IMPORT")]


def test_warnings_and_errors_are_ci_annotations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exceptions = '[size]\n"packages/store/src/pkg/store/big.py" = { most = 900, reason = "#1" }\n'
    files = {"__init__.py": "", "big.py": "x = 1\n" * 801, "a.py": "from . import b\n"}
    root = _package(tmp_path, {**files, "b.py": "from . import a\n"}, exceptions)
    assert structure.main(root) == 1
    out = capsys.readouterr().out
    assert "::warning file=packages/store/src/pkg/store/big.py::FILE_TOO_LARGE packages/" in out
    assert "::error::IMPORT_CYCLE store: pkg.store.a -> pkg.store.b" in out


def test_the_repository_has_no_unlisted_cycle_or_facade_import() -> None:
    assert structure.imports(REPOSITORY_ROOT) == []
