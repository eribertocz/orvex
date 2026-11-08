"""Architecture checks (`python -m tools.checks.boundaries`): module boundaries, run together
with the materialization and data-reader checks.

1. MODULE_BOUNDARY_VIOLATION (here): code in `packages/<module>/` imports only its own module,
   `<namespace>.contracts`, the standard library and third-party packages; `server` composes all.
2. MATERIALIZATION_IN_PYTHON: `tools/checks/materialization.py`.
3. DATA_READ_OUTSIDE_DUCKDB: `tools/checks/data_readers.py`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from tools.checks.architecture import AllowListError, Finding, product_files
from tools.checks.data_readers import (
    READER_ALLOW_LIST,
    load_reader_allow_list,
    reader_findings,
    reader_scan,
    stale_reader_entries,
)
from tools.checks.materialization import (
    ALLOW_LIST,
    MATERIALIZING_NAMES,
    load_allow_list,
    materialization_findings,
    stale_entries,
)
from tools.repository import MODULES, REPOSITORY_ROOT, identity

__all__ = [
    "MATERIALIZING_NAMES",
    "AllowListError",
    "Finding",
    "boundary_findings",
    "imported_modules",
    "load_allow_list",
    "load_reader_allow_list",
    "main",
    "materialization_findings",
    "product_files",
    "reader_findings",
    "reader_scan",
    "run",
    "scan",
    "stale_entries",
    "stale_reader_entries",
]

SHARED_MODULES = frozenset({"contracts"})
COMPOSER = "server"


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
