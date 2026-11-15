"""The absolute imports a Python file makes, as the boundary rules read them: `import`,
`from … import`, and `importlib.import_module` or `__import__` called with a constant name.

Used by the module rule (`boundaries.py`) and the consumer rule (`consumers.py`), and the
dynamic-call test by the import-cycle note (`structure.py`). `tools/checks/module_dependencies.py`
keeps its own small reader on purpose: it runs with `python -I` in a clean environment outside
the repository (#486 G4), where the `tools` package is not importable.
"""

from __future__ import annotations

import ast
from typing import TypeGuard

DYNAMIC = frozenset({"import_module", "__import__"})


def dynamic_import(node: ast.AST) -> TypeGuard[ast.Call]:
    """Whether `node` calls `import_module` or `__import__`, by name or as an attribute."""
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if isinstance(function, ast.Name):
        return function.id in DYNAMIC
    return isinstance(function, ast.Attribute) and function.attr in DYNAMIC


def imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Each absolute name imported, with its line: `a.b` for `import a.b`, both `a` and `a.b`
    for `from a import b`, and the constant first argument of a dynamic import."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
            found.extend((f"{node.module}.{alias.name}", node.lineno) for alias in node.names)
        elif dynamic_import(node) and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.value, node.lineno))
    return found
