"""The standalone job's checks (#486 G4): declared dependencies equal the used ones, and a
module's tests import no repository helper. The planted violations each fail."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.checks.module_dependencies import (
    dependency_problems,
    imported_modules,
    suite_problems,
)
from tools.ci.standalone import constraints, outside_repository
from tools.repository import REPOSITORY_ROOT

OWNERS = {
    "orvex.store": "orvex-store",
    "orvex.store.lake": "orvex-store",
    "orvex.contracts": "orvex-contracts",
    "orvex.contracts.store.v1": "orvex-contracts",
    "duckdb": "duckdb",
    "pyarrow": "pyarrow",
    "pyarrow.compute": "pyarrow",
    "yaml": "PyYAML",
    "pytest": "pytest",
    "tests.support": None,
}


def owner_of(name: str) -> str | None:
    return OWNERS.get(name)


def codes(found: list[str]) -> list[str]:
    return sorted(problem.split(":", 1)[0] for problem in found)


SOURCE = "import json\nimport duckdb\nfrom orvex.contracts.store.v1 import X\nfrom . import y\n"


def test_imports_are_read_with_their_full_names() -> None:
    assert imported_modules(SOURCE + "import pyarrow.compute as pc\n") == {
        "json",
        "duckdb",
        "orvex.contracts.store.v1",
        "pyarrow.compute",
    }


def test_declared_dependencies_equal_to_the_used_ones_pass() -> None:
    imports = imported_modules(SOURCE) | {"orvex.store.lake"}
    declared = ["duckdb", "orvex-contracts"]
    assert dependency_problems("orvex-store", imports, owner_of, declared, declared) == []


def test_a_planted_undeclared_dependency_fails() -> None:
    imports = imported_modules(SOURCE) | {"pyarrow.compute"}
    declared = ["duckdb", "orvex-contracts"]
    found = dependency_problems("orvex-store", imports, owner_of, declared, declared)
    assert codes(found) == ["MODULE_DEPENDENCY_UNDECLARED"]
    assert "pyarrow" in found[0]


def test_an_import_nothing_installed_provides_fails() -> None:
    found = dependency_problems("orvex-store", {"missing_package"}, owner_of, [], [])
    assert codes(found) == ["MODULE_DEPENDENCY_UNDECLARED"]


def test_a_declared_dependency_not_used_fails() -> None:
    declared = ["duckdb", "orvex-contracts", "pyarrow"]
    found = dependency_problems(
        "orvex-store", imported_modules(SOURCE), owner_of, declared, declared
    )
    assert codes(found) == ["MODULE_DEPENDENCY_UNUSED"]


def test_a_manifest_that_disagrees_with_the_distribution_fails() -> None:
    declared = ["duckdb", "orvex-contracts"]
    found = dependency_problems(
        "orvex-store",
        imported_modules(SOURCE),
        owner_of,
        declared,
        ["duckdb", "orvex-contracts", "PyArrow"],
    )
    assert codes(found) == ["MODULE_MANIFEST_DEPENDENCIES_DIFFER"]


@pytest.mark.parametrize(
    "source",
    [
        "from tests.support.access_directory import World\n",
        "import conformance.runners.semantic_corpus\n",
        "from tools.repository import REPOSITORY_ROOT\n",
        "import tests\n",
    ],
)
def test_a_planted_test_import_of_a_repository_helper_fails(source: str) -> None:
    found = suite_problems(
        "orvex-store", {"test_a.py": imported_modules(source)}, owner_of, ["duckdb"], []
    )
    assert codes(found) == ["MODULE_TEST_IMPORTS_REPOSITORY"]
    assert "test_a.py" in found[0]


def test_test_dependencies_must_be_declared_and_used() -> None:
    imports = {"t.py": {"pytest", "yaml", "duckdb", "orvex.store.lake"}}
    assert suite_problems("orvex-store", imports, owner_of, ["duckdb"], ["pytest", "pyyaml"]) == []
    assert codes(suite_problems("orvex-store", imports, owner_of, ["duckdb"], ["pytest"])) == [
        "MODULE_TEST_DEPENDENCY_UNDECLARED"
    ]
    assert codes(
        suite_problems(
            "orvex-store", imports, owner_of, ["duckdb"], ["pytest", "pyyaml", "hypothesis"]
        )
    ) == ["MODULE_TEST_DEPENDENCY_UNUSED"]


def test_the_job_works_outside_the_repository(tmp_path: Path) -> None:
    assert outside_repository(tmp_path) == tmp_path
    with pytest.raises(SystemExit, match="STANDALONE_INSIDE_REPOSITORY"):
        outside_repository(REPOSITORY_ROOT / "build")


def test_the_environment_is_constrained_to_the_lock() -> None:
    pinned = constraints(REPOSITORY_ROOT / "pylock.toml")
    assert "pydantic==2.14.3" in pinned
    assert all("==" in line for line in pinned)
