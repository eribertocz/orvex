"""The independence of an alternative implementation (#486 G1): it imports only the contracts,
its declared third-party packages, the standard library and itself, and shares no block of code
with the module it replaces.

    python -m tools.checks.alternative_independence

ALTERNATIVE_IMPORT_FORBIDDEN: a file of the alternative (its sources and its tests) imports
anything else, the module it replaces first of all. ALTERNATIVE_CODE_SHARED: `WINDOW`
consecutive lines of code, compared without comments, docstrings, imports or spacing, appear
both in the alternative's sources and in the replaced module's sources.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

from tools.checks.architecture import Finding
from tools.checks.imports import dynamic_import, imported_names
from tools.repository import REPOSITORY_ROOT, identity

WINDOW = 4  # consecutive normalized lines that make a shared block


@dataclass(frozen=True, slots=True)
class Alternative:
    """An alternative, its own package, what it may import and the module it replaces."""

    folder: str
    package: str
    third_party: tuple[str, ...]
    replaces: str


ALTERNATIVES = (
    Alternative(
        "conformance/alternatives/result_cache_ipc",
        "alternative_result_cache_ipc",
        ("pyarrow", "pytest"),
        "packages/semantic",
    ),
)


def _imported(tree: ast.AST) -> list[tuple[str, int]]:
    """Every absolute import, as the boundary rules read them, and a dynamic import of a name
    not written as a constant (`<dynamic>`), which can never be shown to be allowed."""
    found = imported_names(tree)
    for node in ast.walk(tree):
        if dynamic_import(node) and not (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(("<dynamic>", node.lineno))
    return found


def import_findings(
    source: str, relative: str, alternative: Alternative, namespace: str
) -> list[Finding]:
    """The imports of one file of the alternative that it may not make."""
    own = f"{namespace}.{alternative.package}"
    allowed_roots = {*sys.stdlib_module_names, *alternative.third_party}
    findings = []
    reported: set[int] = set()
    for name, line in _imported(ast.parse(source, filename=relative)):
        root = name.split(".")[0]
        if (
            name == f"{namespace}.contracts"
            or name.startswith(f"{namespace}.contracts.")
            or name == own
            or name.startswith(f"{own}.")
            or (root != namespace and root in allowed_roots)
            or line in reported
        ):
            continue
        reported.add(line)
        findings.append(
            Finding(
                "ALTERNATIVE_IMPORT_FORBIDDEN",
                relative,
                line,
                f"imports {name!r}: an alternative imports only the contracts, "
                f"{', '.join(alternative.third_party)}, the standard library and itself",
            )
        )
    return findings


def code_lines(source: str) -> list[tuple[int, str]]:
    """The lines of code of `source`, by line number, without comments, docstrings, imports
    or spacing: what two copies of one block have in common."""
    tree = ast.parse(source)
    skipped: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                skipped.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
        if isinstance(node, ast.Import | ast.ImportFrom):
            skipped.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    text: dict[int, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in {
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
        }:
            continue
        if token.start[0] not in skipped:
            text.setdefault(token.start[0], []).append(token.string)
    return [(number, " ".join(parts)) for number, parts in sorted(text.items())]


def _windows(lines: list[tuple[int, str]]) -> dict[tuple[str, ...], int]:
    return {
        tuple(text for _, text in lines[i : i + WINDOW]): lines[i][0]
        for i in range(len(lines) - WINDOW + 1)
    }


def shared_findings(alternative: dict[str, str], replaced: dict[str, str]) -> list[Finding]:
    """Each block of `WINDOW` lines an alternative's source shares with the replaced module's
    sources (both by relative path), reported once per file and line."""
    known: dict[tuple[str, ...], str] = {}
    for path, source in sorted(replaced.items()):
        for window, line in _windows(code_lines(source)).items():
            known.setdefault(window, f"{path}:{line}")
    findings = []
    for path, source in sorted(alternative.items()):
        for window, line in sorted(_windows(code_lines(source)).items(), key=lambda w: w[1]):
            if window in known:
                findings.append(
                    Finding(
                        "ALTERNATIVE_CODE_SHARED",
                        path,
                        line,
                        f"{WINDOW} lines of code are the same as at {known[window]}",
                    )
                )
    return findings


def _sources(root: Path, folder: str) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((root / folder).rglob("*.py"))
        if "__pycache__" not in path.parts and "build" not in path.parts
    }


def independence_scan(root: Path = REPOSITORY_ROOT) -> list[Finding]:
    namespace = str(identity(root)["python_namespace"])
    findings: list[Finding] = []
    for alternative in ALTERNATIVES:
        own = _sources(root, alternative.folder)
        for relative, source in own.items():
            findings += import_findings(source, relative, alternative, namespace)
        sources = {path: text for path, text in own.items() if "/src/" in path}
        findings += shared_findings(sources, _sources(root, f"{alternative.replaces}/src"))
    return findings


def main() -> int:
    findings = independence_scan()
    for finding in findings:
        print(f"{finding.code}: {finding.path}:{finding.line}: {finding.message}")
    names = ", ".join(alternative.folder for alternative in ALTERNATIVES)
    print(f"alternative independence: {len(findings)} finding(s) in {names}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
