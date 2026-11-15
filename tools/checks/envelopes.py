"""ENVELOPE_BUILT_BY_HAND (#486 §2 item 6): only `<namespace>/contracts/context.py` builds a call
envelope, so every envelope carries a real `CallContext`.

In product code (`packages/*/src`), the conformance runners and `tools/`, a call of
`CallEnvelope(...)`, or of `dataclasses.replace(...)` in a file that names `CallEnvelope`, is a
finding. Tests may build broken envelopes on purpose, to prove that a binding refuses them.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tools.checks.architecture import Finding
from tools.repository import identity

CODE = "ENVELOPE_BUILT_BY_HAND"
ENVELOPE = "CallEnvelope"
AREAS = ("packages/", "conformance/runners/", "tools/")


def _called(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        prefix = function.value.id if isinstance(function.value, ast.Name) else ""
        return f"{prefix}.{function.attr}" if prefix else function.attr
    return ""


def envelope_findings(tree: ast.AST, relative: str) -> list[Finding]:
    names_envelope = any(
        (isinstance(node, ast.Name) and node.id == ENVELOPE)
        or (isinstance(node, ast.alias) and node.name == ENVELOPE)
        for node in ast.walk(tree)
    )
    replaced = {"dataclasses.replace"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
            replaced |= {
                alias.asname or alias.name for alias in node.names if alias.name == "replace"
            }
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _called(node)
        if called.rpartition(".")[2] == ENVELOPE or (names_envelope and called in replaced):
            findings.append(
                Finding(CODE, relative, node.lineno, "only contracts/context.py builds an envelope")
            )
    return findings


def _product(relative: str) -> bool:
    if relative.startswith("packages/"):
        return "/src/" in relative
    return relative.startswith(AREAS)


def envelope_scan(root: Path) -> list[Finding]:
    namespace = str(identity(root)["python_namespace"])
    builder = f"packages/contracts/src/{namespace}/contracts/context.py"
    findings: list[Finding] = []
    for start in AREAS:
        base = root / start
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative == builder or not _product(relative) or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            findings += envelope_findings(tree, relative)
    return findings
