"""Module structure checks (#246, PR 1), with the standard-library `ast` module.

    python -m tools.checks.structure

1. FILE_TOO_LARGE (a warning, never a failure): a module source file over `LIMIT` lines, unless
   `file-size-exceptions.toml` justifies it in one line. An exception for a file that no longer
   exists or is within the limit is warned too, so the list only holds what it must.
2. IMPORT_CYCLE (a failure): a cycle among the modules of one package (`<namespace>.<module>`),
   through absolute imports, relative imports, and imports inside functions or `if`/`try`
   blocks. Imports under `if TYPE_CHECKING:` are typing-only: a cycle made only of them is not
   reported.
3. FACADE_IMPORTED (a failure): an internal module importing its package's façade (`store.lake`,
   `server.commands`). A cycle or a façade import that exists before its split (#246, PR 2 and
   3) is listed in `file-size-exceptions.toml` (`[cycle]`, `[facade]`) with the issue that removes
   it; an entry that no longer applies fails, so the lists only shrink.
4. DYNAMIC_IMPORT (a note): `importlib.import_module` or `__import__` in module code, which no
   static check can follow: listed so a reviewer sees it.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tools.repository import REPOSITORY_ROOT

LIMIT = 800
EXCEPTIONS = Path(__file__).with_name("file-size-exceptions.toml")
FACADES = {"store": "lake", "server": "commands"}


@dataclass(frozen=True, slots=True)
class Finding:
    level: str  # "warning", "error" or "note"
    code: str
    path: str
    text: str

    def __str__(self) -> str:
        return f"{self.level}: {self.code} {self.path}: {self.text}"


def _sources(root: Path) -> Iterator[tuple[str, Path]]:
    """Each module package's source files, with the package (`store`, `server`, …)."""
    for path in sorted(root.glob("packages/*/src/*/*/**/*.py")):
        relative = path.relative_to(root)
        yield relative.parts[1], path


def _exceptions(root: Path) -> dict[str, dict[str, str]]:
    file = root / EXCEPTIONS.relative_to(REPOSITORY_ROOT)
    if not file.is_file():
        return {"size": {}, "facade": {}, "cycle": {}}
    document = tomllib.loads(file.read_text("utf-8"))
    return {key: dict(document.get(key, {})) for key in ("size", "facade", "cycle")}


def sizes(root: Path) -> list[Finding]:
    justified = _exceptions(root)["size"]
    found = []
    counted: dict[str, int] = {}
    for _, path in _sources(root):
        relative = path.relative_to(root).as_posix()
        counted[relative] = len(path.read_text("utf-8").splitlines())
    for relative, lines in counted.items():
        if lines > LIMIT and relative not in justified:
            found.append(
                Finding("warning", "FILE_TOO_LARGE", relative, f"{lines} lines (limit {LIMIT})")
            )
    for relative in sorted(justified):
        if counted.get(relative, 0) <= LIMIT:
            found.append(
                Finding(
                    "warning",
                    "FILE_TOO_LARGE",
                    relative,
                    "listed as an exception, but missing or within the limit: remove the entry",
                )
            )
    return found


@dataclass(frozen=True, slots=True)
class Edge:
    target: str
    typing_only: bool


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).parts[3:])  # packages/<p>/src/<ns>/<module>/...
    parts[-1] = parts[-1].removesuffix(".py")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _typing_blocks(tree: ast.Module) -> set[int]:
    """The ids of import nodes under `if TYPE_CHECKING:`."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (
            (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
            or (isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING")
        ):
            for inner in node.body:
                for child in ast.walk(inner):
                    if isinstance(child, ast.Import | ast.ImportFrom):
                        found.add(id(child))
    return found


def _edges(name: str, path: Path, known: set[str]) -> list[Edge]:
    """The modules of the same package that `name` imports, anywhere in the file."""
    tree = ast.parse(path.read_text("utf-8"))
    typing = _typing_blocks(tree)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: list[Edge] = []

    def add(target: str, node: ast.AST) -> None:
        while target and target not in known:
            target = target.rpartition(".")[0]  # `a.b.c` names a function or class of `a.b`
        if target and target != name:
            found.append(Edge(target, id(node) in typing))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, node)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                stem = ".".join([*base, *([node.module] if node.module else [])])
            else:
                stem = node.module or ""
            for alias in node.names:
                candidate = f"{stem}.{alias.name}"
                add(candidate if candidate in known else stem, node)
    return found


def graph(root: Path) -> dict[str, dict[str, list[Edge]]]:
    """Per package, each module and the modules of the same package it imports."""
    files: dict[str, dict[str, Path]] = {}
    for package, path in _sources(root):
        files.setdefault(package, {})[_module_name(root, path)] = path
    return {
        package: {name: _edges(name, path, set(modules)) for name, path in modules.items()}
        for package, modules in files.items()
    }


def _cycles(edges: dict[str, list[Edge]]) -> list[list[str]]:
    """Strongly connected groups of more than one module, over runtime edges (Tarjan)."""
    runtime = {n: sorted({e.target for e in es if not e.typing_only}) for n, es in edges.items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    groups: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on.add(node)
        for target in runtime.get(node, ()):
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            group = []
            while True:
                top = stack.pop()
                on.discard(top)
                group.append(top)
                if top == node:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for node in sorted(runtime):
        if node not in index:
            visit(node)
    return sorted(groups)


def imports(root: Path) -> list[Finding]:
    exceptions = _exceptions(root)
    allowed, cycles = exceptions["facade"], exceptions["cycle"]
    found: list[Finding] = []
    seen_cycles: set[str] = set()
    seen_facades: set[str] = set()
    for package, edges in graph(root).items():
        for group in _cycles(edges):
            text = " -> ".join(group)
            seen_cycles.add(text)
            if text not in cycles:
                found.append(Finding("error", "IMPORT_CYCLE", package, text))
        facade = FACADES.get(package)
        if facade is None:
            continue
        for name, targets in edges.items():
            if name.endswith(f".{facade}") or name.count(".") < 2:
                continue  # the façade itself, and the package's `__init__`
            if any(e.target.endswith(f".{package}.{facade}") for e in targets):
                seen_facades.add(name)
                if name not in allowed:
                    found.append(
                        Finding("error", "FACADE_IMPORTED", name, f"imports {package}.{facade}")
                    )
    # An exception that no longer applies fails, so the lists only shrink as the split lands.
    for text in sorted(set(cycles) - seen_cycles):
        found.append(Finding("error", "IMPORT_CYCLE", text, "listed, but gone: remove it"))
    for name in sorted(set(allowed) - seen_facades):
        found.append(Finding("error", "FACADE_IMPORTED", name, "listed, but gone: remove it"))
    return found


def dynamic(root: Path) -> list[Finding]:
    found = []
    for _, path in _sources(root):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            ):
                found.append(
                    Finding(
                        "note",
                        "DYNAMIC_IMPORT",
                        f"{path.relative_to(root).as_posix()}:{node.lineno}",
                        "not followed by the cycle check: review it",
                    )
                )
    return found


def main() -> int:
    findings = [*sizes(REPOSITORY_ROOT), *imports(REPOSITORY_ROOT), *dynamic(REPOSITORY_ROOT)]
    for finding in findings:
        if finding.level == "warning":  # an annotation in CI, never a failure
            print(f"::warning file={finding.path}::{finding.code} {finding.text}")
        print(finding)
    return 1 if any(f.level == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
