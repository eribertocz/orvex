"""Architecture checks with the standard-library `ast` module.

1. MODULE_BOUNDARY_VIOLATION: code in `packages/<module>/` imports only its own module,
   `<namespace>.contracts`, the standard library and third-party packages; `server` composes all.
2. MATERIALIZATION_IN_PYTHON: product code never pulls rows into Python (ADR-0025), unless the
   call is listed in `materialization-allowlist.toml` with a reason.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tools.repository import MODULES, REPOSITORY_ROOT, identity

MATERIALIZING_CALLS = frozenset({"fetchall", "fetchone", "fetchmany", "df", "pl", "to_pandas"})
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
    code = "ALLOWLIST_ENTRY_INVALID"


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
        allowed.add((entry["file"], entry["call"]))
    return allowed


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
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MATERIALIZING_CALLS
            and (relative, node.func.attr) not in allowed
        ):
            findings.append(
                Finding(
                    "MATERIALIZATION_IN_PYTHON",
                    relative,
                    node.lineno,
                    f".{node.func.attr}() reads rows into Python (ADR-0025)",
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


def main() -> int:
    try:
        findings = scan(REPOSITORY_ROOT, load_allow_list())
    except AllowListError as error:
        print(error)
        return 1
    for finding in findings:
        print(finding)
    print(f"boundaries: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
