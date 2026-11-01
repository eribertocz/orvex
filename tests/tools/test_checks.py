import ast
from pathlib import Path

import pytest

from tools.checks.boundaries import (
    MATERIALIZING_NAMES,
    AllowListError,
    boundary_findings,
    load_allow_list,
    materialization_findings,
    stale_entries,
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

    @pytest.mark.parametrize(
        "call",
        [
            "fetchall",
            "fetchone",
            "fetchmany",
            "fetchdf",
            "fetch_df",
            "fetch_df_chunk",
            "fetchnumpy",
            "df",
            "pl",
            "to_df",
            "torch",
            "tf",
            "to_pandas",
            "to_numpy",
            "tolist",
            "to_pylist",
            "to_pydict",
            "as_py",
            "iterrows",
            "itertuples",
        ],
    )
    def test_every_materializing_call_is_reported(self, call: str) -> None:
        assert call in MATERIALIZING_NAMES
        assert len(materialization_findings(parse(f"x.{call}()\n"), "a.py", set())) == 1

    def test_the_list_is_exactly_the_issues(self) -> None:
        # #46: one name per way of turning rows into Python objects, no more and no fewer;
        # #261 adds the row-by-row walks of a DataFrame.
        assert len(MATERIALIZING_NAMES) == 20

    def test_a_reference_without_a_call_is_reported(self) -> None:
        tree = parse("rows = table.to_pylist\nfirst = map(column.as_py, values)\n")
        assert [f.line for f in materialization_findings(tree, "a.py", set())] == [1, 2]

    def test_assigning_an_attribute_of_that_name_is_not_a_read(self) -> None:
        assert materialization_findings(parse("frame.df = None\n"), "a.py", set()) == []

    @pytest.mark.parametrize(
        "call",
        [
            "to_arrow_table",
            "to_arrow_reader",
            "fetch_arrow_table",
            "fetch_record_batch",
            "read_all",
            "read_next_batch",
        ],
    )
    def test_reading_once_into_arrow_is_accepted(self, call: str) -> None:
        # ADR-0025 rule 3: a final result is read once into Arrow.
        assert materialization_findings(parse(f"x.{call}()\n"), "a.py", set()) == []

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

    def test_arrow_to_python_in_tests_is_exempt(self, tmp_path: Path) -> None:
        repository = make_repository(
            tmp_path, {"packages/store/tests/test_a.py": "def g(t):\n    return t.to_pylist()\n"}
        )
        assert scan_boundaries(repository, set()) == []

    def test_allow_list_entry_without_reason_is_rejected(self, tmp_path: Path) -> None:
        allow_list = tmp_path / "allow.toml"
        allow_list.write_text('[[allow]]\nfile = "a.py"\ncall = "fetchall"\nreason = ""\n')
        with pytest.raises(AllowListError, match="MATERIALIZATION_ALLOW_LIST_INVALID"):
            load_allow_list(allow_list)

    @pytest.mark.parametrize("tracked", ['"soon"', '"#0"', '"46"', "46"])
    def test_a_tracked_entry_must_name_an_issue(self, tmp_path: Path, tracked: str) -> None:
        allow_list = tmp_path / "allow.toml"
        allow_list.write_text(
            '[[allow]]\nfile = "a.py"\ncall = "fetchall"\nreason = "unbounded"\n'
            f"tracked = {tracked}\n"
        )
        with pytest.raises(AllowListError, match="MATERIALIZATION_ALLOW_LIST_INVALID"):
            load_allow_list(allow_list)

    def test_a_tracked_entry_naming_an_issue_is_accepted(self, tmp_path: Path) -> None:
        allow_list = tmp_path / "allow.toml"
        allow_list.write_text(
            '[[allow]]\nfile = "a.py"\ncall = "fetchall"\nreason = "unbounded"\ntracked = "#46"\n'
        )
        assert load_allow_list(allow_list) == {("a.py", "fetchall")}

    def test_an_entry_matching_no_use_is_stale(self, tmp_path: Path) -> None:
        repository = make_repository(
            tmp_path,
            {"packages/store/src/orvex/store/a.py": "def f(r):\n    return r.fetchone()\n"},
        )
        used = ("packages/store/src/orvex/store/a.py", "fetchone")
        gone = ("packages/store/src/orvex/store/a.py", "fetchall")
        assert stale_entries(repository, {used, gone}) == [gone]

    def test_the_repository_has_no_stale_entries(self) -> None:
        assert stale_entries(REPOSITORY_ROOT, load_allow_list()) == []

    def test_repository_has_no_findings(self) -> None:
        assert scan_boundaries(REPOSITORY_ROOT, load_allow_list()) == []


def readers(source: str, relative: str = "packages/store/src/orvex/store/x.py") -> list[str]:
    from tools.checks.boundaries import reader_findings

    return [f.detail for f in reader_findings(ast.parse(source), relative, set())]


TEST_FILE = "packages/store/tests/test_x.py"


def allow_list(tmp_path: Path, body: str) -> Path:
    (tmp_path / "packages" / "store" / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / TEST_FILE).write_text("def test_copy() -> None:\n    assert True\n")
    path = tmp_path / "allow.toml"
    path.write_text(body)
    return path


ENTRY = (
    '[[allow]]\nfile = "packages/store/src/orvex/store/x.py"\nreader = "sqlite3"\n'
    'function = "_copy"\nreason = "copies the file; reads schema names only"\n'
)


class TestDataReaders:
    """Only DuckDB reads source rows in `store` and `transform` (engineering.md §2, #261)."""

    @pytest.mark.parametrize(
        "source",
        [
            "import pyarrow.parquet as pq\n",
            "from pyarrow import parquet\n",
            "from pyarrow.csv import read_csv\n",
            "import pyarrow.dataset\n",
            "import pyarrow.json\n",
            "import pyarrow.orc\n",
            "from pyarrow import feather\n",
            "import pyarrow as pa\nt = pa.parquet.read_table('f')\n",
            "import pyarrow as pa\nt = pa.csv.read_csv('f')\n",
            "import pyarrow as pa\nd = pa.dataset.dataset('f')\n",
            "import pyarrow as pa\nt = pa.ipc.open_file('f').read_all()\n",
            "from pyarrow import ipc\nt = ipc.open_stream('f')\n",
            "import pyarrow as pa\nr = pa.ipc.RecordBatchFileReader(pa.memory_map('f'))\n",
            "import pyarrow as pa\nf = pa.OSFile('f')\n",
            "import pyarrow as pa\nf = pa.input_stream('f')\n",
            "import pyarrow.fs\n",
            "import csv\n",
            "import sqlite3\n",
            "import pandas\n",
            "import polars as pl\n",
            "import fastparquet\n",
            "text = open('source.csv').read()\n",
            "import os\nd = os.open('source.csv', 0)\n",
            "import os\nh = os.fdopen(3)\n",
            "import io\nh = io.open('source.csv')\n",
            "from pathlib import Path\nt = Path('source.csv').read_text()\n",
            "def f(p):\n    return p.read_bytes()\n",
        ],
    )
    def test_a_reader_other_than_duckdb_is_reported(self, source: str) -> None:
        assert readers(source)

    @pytest.mark.parametrize(
        "relative",
        ["packages/store/src/orvex/store/x.py", "packages/transform/src/orvex/transform/x.py"],
    )
    def test_store_and_transform_are_checked(self, relative: str) -> None:
        assert readers("import sqlite3\n", relative)

    @pytest.mark.parametrize(
        "relative",
        [
            "packages/server/src/orvex/server/x.py",
            "packages/store/tests/test_x.py",
            "packages/transform/tests/test_x.py",
        ],
    )
    def test_other_modules_and_tests_are_not_checked(self, relative: str) -> None:
        assert not readers("import pyarrow.parquet as pq\n", relative)

    def test_arrow_as_a_transport_is_accepted(self) -> None:
        source = (
            "import pyarrow as pa\n"
            "def stream(result) -> pa.RecordBatchReader:\n"
            "    return result.to_arrow_reader(10_000)\n"
            "def schema(fields) -> pa.Schema:\n"
            "    return pa.schema(fields)\n"
            "def batches(schema, items) -> pa.RecordBatchReader:\n"
            "    return pa.RecordBatchReader.from_batches(schema, items)\n"
            "def write(sink, schema):\n"
            "    return pa.ipc.new_stream(sink, schema)\n"
        )
        assert readers(source) == []

    def test_an_exception_is_limited_to_its_function(self) -> None:
        from tools.checks.boundaries import reader_findings

        source = (
            "import sqlite3\n"
            "def _copy(path):\n    return sqlite3.connect(path)\n"
            "def other(path):\n    return sqlite3.connect(path)\n"
        )
        allowed = {("packages/store/src/orvex/store/x.py", "sqlite3", "_copy")}
        found = reader_findings(ast.parse(source), "packages/store/src/orvex/store/x.py", allowed)
        assert [f.line for f in found] == [5]

    def test_an_exception_needs_a_reason_a_function_and_a_test_in_a_test_folder(
        self, tmp_path: Path
    ) -> None:
        from tools.checks.boundaries import AllowListError, load_reader_allow_list

        good = allow_list(tmp_path, ENTRY + f'test = "{TEST_FILE}::test_copy"\n')
        assert load_reader_allow_list(good, root=tmp_path) == {
            ("packages/store/src/orvex/store/x.py", "sqlite3", "_copy")
        }
        bad = [
            ENTRY.replace('reason = "copies the file; reads schema names only"\n', "")
            + f'test = "{TEST_FILE}::test_copy"\n',  # no reason
            ENTRY.replace('function = "_copy"\n', "") + f'test = "{TEST_FILE}::test_copy"\n',
            ENTRY,  # no test
            ENTRY + f'test = "{TEST_FILE}::test_gone"\n',  # a test that does not exist
            ENTRY + 'test = "packages/store/src/orvex/store/x.py::_copy"\n',  # not a test
            ENTRY + f'test = "{(tmp_path / TEST_FILE).as_posix()}::test_copy"\n',  # absolute
            ENTRY + 'test = "../outside/test_x.py::test_copy"\n',  # outside the repository
        ]
        for body in bad:
            with pytest.raises(AllowListError):
                load_reader_allow_list(allow_list(tmp_path, body), root=tmp_path)

    def test_an_exception_matching_no_use_is_stale(self, tmp_path: Path) -> None:
        from tools.checks.boundaries import stale_reader_entries

        repository = make_repository(
            tmp_path, {"packages/store/src/orvex/store/x.py": "import duckdb\n"}
        )
        allowed = {("packages/store/src/orvex/store/x.py", "sqlite3", "_copy")}
        assert stale_reader_entries(repository, allowed) == sorted(allowed)

    def test_the_check_fails_on_a_reader_and_passes_without_one(self, tmp_path: Path) -> None:
        from tools.checks.boundaries import run

        files = {"packages/store/src/orvex/store/x.py": "import pyarrow.parquet\n"}
        empty = tmp_path / "empty.toml"
        empty.write_text("")
        (tmp_path / "bad").mkdir()
        (tmp_path / "good").mkdir()
        assert run(make_repository(tmp_path / "bad", files), empty, empty) == 1
        clean = {"packages/store/src/orvex/store/x.py": "import duckdb\n"}
        assert run(make_repository(tmp_path / "good", clean), empty, empty) == 0

    def test_a_stale_exception_fails_the_check(self, tmp_path: Path) -> None:
        from tools.checks.boundaries import run

        repository = make_repository(
            tmp_path, {"packages/store/src/orvex/store/x.py": "import duckdb\n"}
        )
        allowed = allow_list(repository, ENTRY + f'test = "{TEST_FILE}::test_copy"\n')
        empty = tmp_path / "empty.toml"
        empty.write_text("")
        assert run(repository, empty, allowed) == 1

    def test_the_repository_has_no_reader_findings_and_no_stale_entries(self) -> None:
        from tools.checks.boundaries import (
            load_reader_allow_list,
            reader_scan,
            stale_reader_entries,
        )

        allowed = load_reader_allow_list()
        assert reader_scan(REPOSITORY_ROOT, allowed) == []
        assert stale_reader_entries(REPOSITORY_ROOT, allowed) == []


class TestUncountedFiles:
    """The size gate's uncounted list matches lock and generated files exactly (#261)."""

    def test_every_uncounted_tracked_file_is_a_lock_or_a_generated_file(self) -> None:
        import subprocess
        import sys

        sys.path.insert(0, str(REPOSITORY_ROOT / ".github" / "workflows" / "scripts"))
        from governance import UNCOUNTED_HEADING, parse_path_block

        from tools.contracts.generate import generated_files_on_disk

        rules = (REPOSITORY_ROOT / "docs" / "process" / "git-workflow.md").read_text()
        uncounted = parse_path_block(rules, UNCOUNTED_HEADING)
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
        generated = {p.relative_to(REPOSITORY_ROOT).as_posix() for p in generated_files_on_disk()}
        locks = {"pylock.toml", "pylock.py314.toml", "web/package-lock.json"}
        wrongly = [p for p in tracked if uncounted.is_governed(p) and p not in generated | locks]
        assert wrongly == []
        assert all(uncounted.is_governed(p) for p in generated | locks)
