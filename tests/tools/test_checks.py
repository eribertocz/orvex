import ast
from pathlib import Path

import pytest

from tools.checks.boundaries import (
    AllowListError,
    boundary_findings,
    load_allow_list,
    materialization_findings,
)
from tools.checks.boundaries import (
    scan as scan_boundaries,
)
from tools.checks.product_name import scan as scan_product_name
from tools.repository import REPOSITORY_ROOT

IDENTITY = """[product]
name = "Orvex"
slug = "orvex"
python_namespace = "orvex"
ossie_vendor_name = "ORVEX"
previous_vendor_names = []
"""


def make_repository(root: Path, files: dict[str, str]) -> Path:
    (root / "identity.toml").write_text(IDENTITY)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


class TestProductName:
    def test_hardcoded_name_in_python_is_reported(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path, {"packages/store/src/x.py": 'TITLE = "Orvex"\n'})
        findings = scan_product_name(repository)
        assert [(f.path, f.line) for f in findings] == [("packages/store/src/x.py", 1)]
        assert str(findings[0]).startswith("PRODUCT_NAME_HARDCODED: packages/store/src/x.py:1:")

    def test_hardcoded_name_in_web_source_is_reported(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path, {"web/src/App.svelte": "<h1>orvex</h1>\n"})
        assert len(scan_product_name(repository)) == 1

    def test_environment_prefix_is_reported(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path, {"tools/x.py": 'HOME = "ORVEX_HOME"\n'})
        assert len(scan_product_name(repository)) == 1

    def test_documentation_is_exempt(self, tmp_path: Path) -> None:
        repository = make_repository(tmp_path, {"docs/x.md": "Orvex is great.\n"})
        assert scan_product_name(repository) == []

    def test_namespace_paths_and_distribution_names_are_allowed(self, tmp_path: Path) -> None:
        source = (
            "import orvex.store\n"
            "from orvex.server import identity\n"
            'NAME = "orvex-semantic"\n'
            'PATH = "src/orvex/transform"\n'
        )
        repository = make_repository(tmp_path, {"packages/server/src/x.py": source})
        assert scan_product_name(repository) == []

    def test_repository_has_no_findings(self) -> None:
        assert scan_product_name(REPOSITORY_ROOT) == []


def parse(source: str) -> ast.AST:
    return ast.parse(source)


class TestBoundaries:
    def test_module_importing_another_module_is_reported(self) -> None:
        tree = parse("from orvex.semantic import engine\n")
        findings = boundary_findings(tree, "transform", "orvex", "packages/transform/src/a.py")
        assert [(f.code, f.line) for f in findings] == [("MODULE_BOUNDARY_VIOLATION", 1)]
        assert "packages/transform/src/a.py:1" in str(findings[0])

    def test_plain_import_is_reported(self) -> None:
        findings = boundary_findings(parse("import orvex.access\n"), "store", "orvex", "f.py")
        assert len(findings) == 1

    def test_from_namespace_import_module_is_reported(self) -> None:
        findings = boundary_findings(parse("from orvex import semantic\n"), "store", "orvex", "f")
        assert len(findings) == 1

    def test_server_may_import_every_module(self) -> None:
        tree = parse("import orvex.semantic\nfrom orvex.store import x\n")
        assert boundary_findings(tree, "server", "orvex", "packages/server/src/a.py") == []

    def test_own_module_contracts_and_third_party_are_allowed(self) -> None:
        tree = parse("import orvex.store.core\nimport orvex.contracts.x\nimport duckdb\n")
        assert boundary_findings(tree, "store", "orvex", "f.py") == []


class TestMaterialization:
    def test_fetchall_in_product_code_is_reported(self) -> None:
        findings = materialization_findings(parse("rows = relation.fetchall()\n"), "a.py", set())
        assert [(f.code, f.line) for f in findings] == [("MATERIALIZATION_IN_PYTHON", 1)]

    @pytest.mark.parametrize("call", ["fetchone", "fetchmany", "df", "pl", "to_pandas"])
    def test_every_materializing_call_is_reported(self, call: str) -> None:
        assert len(materialization_findings(parse(f"x.{call}()\n"), "a.py", set())) == 1

    def test_allow_listed_call_passes(self) -> None:
        tree = parse("x.fetchone()\n")
        assert materialization_findings(tree, "a.py", {("a.py", "fetchone")}) == []

    def test_tests_are_exempt(self, tmp_path: Path) -> None:
        repository = make_repository(
            tmp_path,
            {
                "packages/store/src/orvex/store/a.py": "def f(r):\n    return r.fetchall()\n",
                "packages/store/tests/test_a.py": "def g(r):\n    return r.fetchall()\n",
            },
        )
        findings = scan_boundaries(repository, set())
        assert [f.path for f in findings] == ["packages/store/src/orvex/store/a.py"]

    def test_allow_list_entry_without_reason_is_rejected(self, tmp_path: Path) -> None:
        allow_list = tmp_path / "allow.toml"
        allow_list.write_text('[[allow]]\nfile = "a.py"\ncall = "fetchall"\nreason = ""\n')
        with pytest.raises(AllowListError, match="ALLOWLIST_ENTRY_INVALID"):
            load_allow_list(allow_list)

    def test_repository_has_no_findings(self) -> None:
        assert scan_boundaries(REPOSITORY_ROOT, load_allow_list()) == []
