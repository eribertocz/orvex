"""Consumer boundaries (#486 G2): `server`, the conformance runners, `tools/` and the repository
tests import only `<namespace>.contracts`; `server/assembly.py` may also import the modules'
`api` packages, and the server's own files the server. Tests (`tests/`, a package's `tests/`)
may also import the shared test helpers, `<namespace>.testing`; nothing else may.

CONSUMER_BOUNDARY_VIOLATION: any other import of a module, as `tools/checks/imports.py` reads
it (static, or dynamic with a constant name). In report mode until #486 step 7:
`python -m tools.checks.boundaries` prints every violation and a summary per area, and fails
only on the module boundaries.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tools.checks.architecture import Finding
from tools.checks.imports import imported_names
from tools.repository import MODULES, TESTING, identity

CODE = "CONSUMER_BOUNDARY_VIOLATION"
# Each consumer area, by the start of its paths.
AREAS = {
    "server": "packages/server/",
    "runners": "conformance/runners/",
    "tools": "tools/",
    "tests": "tests/",
}
SHARED = "contracts"
PUBLIC = "api"


def assembly(namespace: str) -> str:
    return f"packages/server/src/{namespace}/server/assembly.py"


def _is_test(relative: str) -> bool:
    return relative.startswith("tests/") or "/tests/" in relative


def _area(relative: str) -> str | None:
    return next((area for area, start in AREAS.items() if relative.startswith(start)), None)


def consumer_findings(tree: ast.AST, namespace: str, relative: str) -> list[Finding]:
    """The internal imports of one consumer file, one per module and line."""
    area = _area(relative)
    if area is None:
        return []
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for name, line in imported_names(tree):
        parts = name.split(".")
        if len(parts) < 2 or parts[0] != namespace or parts[1] not in (*MODULES, TESTING):
            continue
        target = parts[1]
        allowed = (
            target == SHARED
            or (target == TESTING and _is_test(relative))
            or (area == "server" and target == "server")
            or (relative == assembly(namespace) and len(parts) > 2 and parts[2] == PUBLIC)
        )
        if allowed or (target, line) in reported:
            continue
        reported.add((target, line))
        findings.append(
            Finding(CODE, relative, line, f"imports '{name}', an internal of '{target}'")
        )
    return findings


def consumer_scan(root: Path) -> list[Finding]:
    namespace = str(identity(root)["python_namespace"])
    findings: list[Finding] = []
    for start in AREAS.values():
        base = root / start
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if any(part in {"build", "__pycache__", "node_modules"} for part in path.parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            findings += consumer_findings(tree, namespace, relative)
    return findings


def consumer_summary(findings: list[Finding]) -> str:
    """One line: how many violations remain, in how many files, per area."""
    files = {finding.path for finding in findings}
    per_area = Counter(_area(path) for path in files)
    areas = ", ".join(f"{area} {per_area.get(area, 0)}" for area in AREAS)
    return (
        f"consumer boundaries (report mode until #486 step 7): {len(findings)} finding(s) "
        f"in {len(files)} file(s): {areas}"
    )
