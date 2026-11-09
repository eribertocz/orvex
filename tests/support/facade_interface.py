"""#246 item 5: the names a façade module makes importable, as the interface snapshot tests of
the store (`test_lake_interface.py`) and the server (`test_commands_interface.py`) record them.

- The public group: the names the façade exports (defined there, or imported as `NAME as NAME`),
  each with its shape (a function's signature, a class's public methods and fields, a
  constant's value).
- The internal group: the underscore names bound in the façade (compatibility only, never a
  promise).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import ModuleType
from typing import TypeAliasType


def exported(module: ModuleType) -> list[str]:
    """The public names `module` exports: defined at its top level, or `import NAME as NAME`."""
    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef | ast.TypeAlias):
            target = node.name if not isinstance(node, ast.TypeAlias) else node.name.id
            names.add(target)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names |= {alias.name for alias in node.names if alias.asname == alias.name}
    return sorted(name for name in names if not name.startswith("_"))


def internal(module: ModuleType) -> list[str]:
    """The underscore names bound in `module`, dunders aside."""
    return sorted(
        name for name in vars(module) if name.startswith("_") and not name.startswith("__")
    )


def _value(value: object) -> str:
    if isinstance(value, frozenset | set):
        return "{" + ", ".join(sorted(repr(item) for item in value)) + "}"
    return repr(value)


def shape(value: object) -> object:
    """What a caller relies on: signatures, public methods and fields, or the value."""
    if isinstance(value, TypeAliasType):
        return {"alias": str(value.__value__)}
    if inspect.isclass(value):
        methods = {
            name: str(inspect.signature(member))
            for name, member in sorted(vars(value).items())
            if (not name.startswith("_") or name == "__init__") and inspect.isfunction(member)
        }
        fields = list(getattr(value, "__dataclass_fields__", None) or ())
        fields += list(getattr(value, "_fields", ()))
        return {"class": methods, "fields": sorted(fields)}
    if callable(value):
        return {"function": str(inspect.signature(value))}
    return {"value": _value(value)}


def interface(module: ModuleType) -> dict[str, object]:
    return {
        "public": {name: shape(getattr(module, name)) for name in exported(module)},
        "internal": internal(module),
    }
