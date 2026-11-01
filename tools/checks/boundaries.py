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
# Readers of source files or databases other than DuckDB; a name covers its submodules.
READER_MODULES = frozenset(
    {
        "pyarrow.parquet",
        "pyarrow.csv",
        "pyarrow.dataset",
        "pyarrow.json",
        "pyarrow.orc",
        "pyarrow.feather",
        "csv",
        "sqlite3",
        "pandas",
        "polars",
        "fastparquet",
    }
)
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
) -> set[tuple[str, str]]:
    """(file, reader) pairs allowed to use a reader other than DuckDB. Each entry names its
    reason and an existing test, `path::name`, that proves it stays within that reason."""
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
    allowed: set[tuple[str, str]] = set()
    for entry in entries:
        if not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("file", "reader", "reason", "test")
        ):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r} needs file, reader, reason and test"
            )
        test_file, _, test_name = entry["test"].partition("::")
        source = root / test_file
        if not (
            test_name
            and source.is_file()
            and re.search(rf"^\s*def {re.escape(test_name)}\(", source.read_text(), re.M)
        ):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r}: test {entry['test']} does not exist"
            )
        allowed.add((entry["file"], entry["reader"]))
    return allowed


def _reader(name: str) -> str | None:
    """The reader module `name` is or belongs to, or None."""
    for module in READER_MODULES:
        if name == module or name.startswith(module + "."):
            return module
    return None


def reader_findings(tree: ast.AST, relative: str, allowed: set[tuple[str, str]]) -> list[Finding]:
    """Uses of a reader other than DuckDB in `store` and `transform` product code."""
    if not relative.startswith(READER_CHECKED):
        return []
    aliases: dict[str, str] = {}  # local name -> the module or object it stands for
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name] = full
                found.append((full, node.lineno))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            current: ast.expr = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name) and current.id in aliases:
                dotted = ".".join([aliases[current.id], *reversed(parts)])
                found.append((dotted, node.lineno))
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for name, line in found:
        module = _reader(name)
        if module is None or (relative, module) in allowed or (module, line) in reported:
            continue
        reported.add((module, line))
        findings.append(
            Finding(
                "DATA_READ_OUTSIDE_DUCKDB",
                relative,
                line,
                f"{module} reads source rows outside DuckDB (engineering.md §2)",
            )
        )
    return findings


def reader_scan(root: Path, allowed: set[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for path, relative in product_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        findings += reader_findings(tree, relative, allowed)
    return findings


def stale_reader_entries(root: Path, allowed: set[tuple[str, str]]) -> list[tuple[str, str]]:
    used = {(f.path, f.detail.split()[0]) for f in reader_scan(root, set())}
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


def main() -> int:
    try:
        allowed = load_allow_list()
        readers = load_reader_allow_list()
    except AllowListError as error:
        print(error)
        return 1
    stale = stale_entries(REPOSITORY_ROOT, allowed) + stale_reader_entries(REPOSITORY_ROOT, readers)
    for file, call in stale:
        print(f"{AllowListError.code}: entry ({file}, {call}) matches no use; remove it")
    if stale:
        return 1
    findings = scan(REPOSITORY_ROOT, allowed) + reader_scan(REPOSITORY_ROOT, readers)
    for finding in findings:
        print(finding)
    print(f"boundaries: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
