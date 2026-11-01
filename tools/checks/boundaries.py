"""Architecture checks with the standard-library `ast` module.

1. MODULE_BOUNDARY_VIOLATION: code in `packages/<module>/` imports only its own module,
   `<namespace>.contracts`, the standard library and third-party packages; `server` composes all.
2. MATERIALIZATION_IN_PYTHON: product code never pulls rows into Python objects (ADR-0025), unless
   the (file, name) is listed in `materialization-allowlist.toml` with a reason that states its
   bound, or with the issue (`tracked`) that removes an unbounded use. Reading a result once into
   Arrow (`to_arrow_table`, `to_arrow_reader`, `fetch_arrow_table`, `fetch_record_batch`,
   `read_all`, `read_next_batch`) is allowed by ADR-0025 rule 3 and not reported.
3. DATA_READ_OUTSIDE_DUCKDB: in `store` and `transform`, only DuckDB reads the rows of a source
   (`engineering.md` §2). Every use of another reader of files or databases is reported: an
   import, or an attribute reached through an imported package (`pa.parquet.read_table`).
   Arrow as a transport (schemas, record batches, readers DuckDB produces, IPC streams) is not
   a reader and is allowed. An exception is listed in `data-reader-allowlist.toml` with its
   reason and the test (`path::name`) that proves it stays within that reason.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tools.repository import MODULES, REPOSITORY_ROOT, identity

# Every way of turning query rows into Python objects (ADR-0025 rules 1 and 6).
MATERIALIZING_NAMES = frozenset(
    {
        # DuckDB results and relations
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
        # DataFrame and array conversions
        "to_pandas",
        "to_numpy",
        "tolist",
        # Arrow to Python
        "to_pylist",
        "to_pydict",
        "as_py",
        # DataFrame rows walked one by one
        "iterrows",
        "itertuples",
    }
)
# Readers of source files or databases other than DuckDB; a name covers what is under it
# (`pyarrow.parquet.read_table`). File openers are readers too: a source path opened in Python
# cannot be told from a project file statically, so each opener is an explicit exception.
READER_MODULES = frozenset(
    {
        "pyarrow.parquet",
        "pyarrow.csv",
        "pyarrow.dataset",
        "pyarrow.json",
        "pyarrow.orc",
        "pyarrow.feather",
        "pyarrow.fs",
        "pyarrow.ipc.open_file",
        "pyarrow.ipc.open_stream",
        "pyarrow.ipc.RecordBatchFileReader",
        "pyarrow.ipc.RecordBatchStreamReader",
        "pyarrow.ipc.read_message",
        "pyarrow.ipc.read_record_batch",
        "pyarrow.ipc.read_schema",
        "pyarrow.ipc.read_tensor",
        "pyarrow.memory_map",
        "pyarrow.OSFile",
        "pyarrow.input_stream",
        "csv",
        "sqlite3",
        "pandas",
        "polars",
        "fastparquet",
        "os.open",
        "os.fdopen",
        "io.open",
    }
)
# Opening calls reached without an import: the built-in and file-reading methods.
READER_BUILTINS = frozenset({"open"})
READER_METHODS = frozenset({"read_text", "read_bytes"})
READER_CHECKED = ("packages/store/src/", "packages/transform/src/")
READER_ALLOW_LIST = Path(__file__).with_name("data-reader-allowlist.toml")
TRACKED = re.compile(r"#[1-9][0-9]*")
SHARED_MODULES = frozenset({"contracts"})
COMPOSER = "server"
ALLOW_LIST = Path(__file__).with_name("materialization-allowlist.toml")


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    path: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.path}:{self.line}: {self.detail}"


class AllowListError(ValueError):
    code = "MATERIALIZATION_ALLOW_LIST_INVALID"


def load_allow_list(path: Path = ALLOW_LIST) -> set[tuple[str, str]]:
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
    allowed: set[tuple[str, str]] = set()
    for entry in entries:
        if not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("file", "call", "reason")
        ):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r} needs file, call and reason"
            )
        tracked = entry.get("tracked")
        if tracked is not None and not (isinstance(tracked, str) and TRACKED.fullmatch(tracked)):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r}: tracked must be an issue, #<number>"
            )
        allowed.add((entry["file"], entry["call"]))
    return allowed


def load_reader_allow_list(
    path: Path = READER_ALLOW_LIST, root: Path = REPOSITORY_ROOT
) -> set[tuple[str, str, str]]:
    """(file, reader, function) triples allowed to use a reader other than DuckDB: only inside
    that function (the module-level import of the reader is allowed with it). Each entry names
    its reason and a test that proves it stays within that reason: `path::test_name`, a path
    relative to the repository, inside a tests folder, defining that test."""
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
    allowed: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("file", "reader", "function", "reason", "test")
        ):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r} needs file, reader, function, reason "
                "and test"
            )
        test_file, _, test_name = entry["test"].partition("::")
        relative = Path(test_file)
        source = (root / relative).resolve()
        in_tests = relative.parts[:1] == ("tests",) or (
            len(relative.parts) > 2
            and relative.parts[0] == "packages"
            and relative.parts[2] == "tests"
        )
        if not (
            not relative.is_absolute()
            and ".." not in relative.parts
            and in_tests
            and source.is_relative_to(root.resolve())
            and test_name.startswith("test_")
            and source.is_file()
            and re.search(rf"^\s*def {re.escape(test_name)}\(", source.read_text(), re.M)
        ):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r}: {entry['test']} is not a test of the "
                "repository's tests folders"
            )
        allowed.add((entry["file"], entry["reader"], entry["function"]))
    return allowed


def _reader(name: str) -> str | None:
    """The reader `name` is or belongs to, or None."""
    for module in READER_MODULES:
        if name == module or name.startswith(module + "."):
            return module
    return None


def _uses(tree: ast.AST) -> list[tuple[str, int, str]]:
    """(dotted name, line, enclosing function or "") of every import, imported attribute,
    built-in opener call and file-reading method call."""
    # `ast.walk` meets an outer function before its inner ones, so the innermost name wins.
    functions: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if inner is not node:
                    functions[id(inner)] = node.name
    aliases: dict[str, str] = {}
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        where = functions.get(id(node), "")
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
                found.append((alias.name, node.lineno, where))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name] = full
                found.append((full, node.lineno, where))
    for node in ast.walk(tree):
        where = functions.get(id(node), "")
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            current: ast.expr = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name) and current.id in aliases:
                found.append(
                    (".".join([aliases[current.id], *reversed(parts)]), node.lineno, where)
                )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in READER_BUILTINS:
                found.append((node.func.id, node.lineno, where))
            elif isinstance(node.func, ast.Attribute) and node.func.attr in READER_METHODS:
                found.append((node.func.attr, node.lineno, where))
    return found


def reader_findings(
    tree: ast.AST, relative: str, allowed: set[tuple[str, str, str]]
) -> list[Finding]:
    """Uses of a reader other than DuckDB in `store` and `transform` product code."""
    if not relative.startswith(READER_CHECKED):
        return []
    excepted = {(reader, function) for file, reader, function in allowed if file == relative}
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for name, line, where in _uses(tree):
        reader = name if name in READER_BUILTINS | READER_METHODS else _reader(name)
        if reader is None or (reader, line) in reported:
            continue
        # An exception covers its function, and the module-level import of its reader.
        if (reader, where) in excepted or (not where and any(r == reader for r, _ in excepted)):
            continue
        reported.add((reader, line))
        findings.append(
            Finding(
                "DATA_READ_OUTSIDE_DUCKDB",
                relative,
                line,
                f"{reader} reads source rows outside DuckDB (engineering.md §2)"
                + (f" in {where}" if where else ""),
            )
        )
    return findings


def reader_scan(root: Path, allowed: set[tuple[str, str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for path, relative in product_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        findings += reader_findings(tree, relative, allowed)
    return findings


def stale_reader_entries(
    root: Path, allowed: set[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """Exceptions that match no use inside their function, so none outlives the code."""
    used: set[tuple[str, str, str]] = set()
    for finding in reader_scan(root, set()):
        detail = finding.detail.split()
        if " in " in finding.detail:
            used.add((finding.path, detail[0], detail[-1]))
    return sorted(allowed - used)


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
            found.extend((f"{node.module}.{alias.name}", node.lineno) for alias in node.names)
    return found


def boundary_findings(tree: ast.AST, owner: str, namespace: str, relative: str) -> list[Finding]:
    if owner == COMPOSER:
        return []
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for name, line in imported_modules(tree):
        parts = name.split(".")
        if len(parts) < 2 or parts[0] != namespace or parts[1] not in MODULES:
            continue
        target = parts[1]
        if target != owner and target not in SHARED_MODULES and (target, line) not in reported:
            reported.add((target, line))
            findings.append(
                Finding(
                    "MODULE_BOUNDARY_VIOLATION",
                    relative,
                    line,
                    f"module '{owner}' imports '{namespace}.{target}'",
                )
            )
    return findings


def materialization_findings(
    tree: ast.AST, relative: str, allowed: set[tuple[str, str]]
) -> list[Finding]:
    """Every read of a materializing name, called or not (`rows = result.to_pylist` too);
    assigning an attribute of that name (`obj.df = …`) is not a read."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in MATERIALIZING_NAMES
            and (relative, node.attr) not in allowed
        ):
            findings.append(
                Finding(
                    "MATERIALIZATION_IN_PYTHON",
                    relative,
                    node.lineno,
                    f".{node.attr} reads rows into Python (ADR-0025)",
                )
            )
    return findings


def stale_entries(root: Path, allowed: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Allow-list entries that no longer match any use, so permissions cannot outlive the code."""
    used: set[tuple[str, str]] = set()
    for path, relative in product_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        used |= {
            (relative, finding.detail.split()[0].removeprefix("."))
            for finding in materialization_findings(tree, relative, set())
        }
    return sorted(allowed - used)


def product_files(root: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for package in sorted(path for path in (root / "packages").iterdir() if path.is_dir()):
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if "/src/" in relative:
                found.append((path, relative))
    return found


def scan(root: Path, allowed: set[tuple[str, str]]) -> list[Finding]:
    namespace = str(identity(root)["python_namespace"])
    findings: list[Finding] = []
    for package in sorted(path for path in (root / "packages").iterdir() if path.is_dir()):
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            findings += boundary_findings(tree, package.name, namespace, relative)
            if "/src/" in relative:
                findings += materialization_findings(tree, relative, allowed)
    return findings


def run(root: Path, materialization_list: Path, reader_list: Path) -> int:
    """The whole check over `root` with these allow-lists: 0 when clean, 1 otherwise."""
    try:
        allowed = load_allow_list(materialization_list)
        readers = load_reader_allow_list(reader_list, root)
    except AllowListError as error:
        print(error)
        return 1
    stale = [*stale_entries(root, allowed), *stale_reader_entries(root, readers)]
    for entry in stale:
        print(f"{AllowListError.code}: entry {entry} matches no use; remove it")
    if stale:
        return 1
    findings = scan(root, allowed) + reader_scan(root, readers)
    for finding in findings:
        print(finding)
    print(f"boundaries: {len(findings)} finding(s)")
    return 1 if findings else 0


def main() -> int:
    return run(REPOSITORY_ROOT, ALLOW_LIST, READER_ALLOW_LIST)


if __name__ == "__main__":
    sys.exit(main())
