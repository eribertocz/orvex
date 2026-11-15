"""The module manifest `module.toml` (#486 §2.5): the interface contracts a module provides and
requires, each at a version with its capabilities, and the distributions it depends on.

    module = "store"                  # its package path under the namespace

    [dependencies]
    runtime = ["duckdb", "pyarrow"]   # what its code imports, equal to its distribution's
    tests = ["pytest"]                # what its own tests import besides those

    [[provides]]                      # one per contract it implements
    contract = "store"
    version = "1.0"
    capabilities = ["read", "write"]
    implementation = "Store"          # an attribute of the module's `api` package, nothing else

    [[requires]]                      # one per contract it consumes
    contract = "security-context"
    version = "1.0"
    capabilities = ["read"]

Anything else, missing or misspelt is refused with MODULE_MANIFEST_INVALID.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Any

from .interface import NAME
from .interface_errors import InterfaceContractError
from .version import Version

# A module, or an alternative implementation, by its package path under the namespace.
MODULE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
DISTRIBUTION = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Provided:
    contract: str
    version: Version
    capabilities: tuple[str, ...]
    implementation: str


@dataclass(frozen=True, slots=True)
class Required:
    contract: str
    version: Version
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    module: str
    provides: tuple[Provided, ...]
    requires: tuple[Required, ...]
    runtime: tuple[str, ...]
    tests: tuple[str, ...]


def canonical(distribution: str) -> str:
    """A distribution name in its normalized form (PEP 503)."""
    return re.sub(r"[-_.]+", "-", distribution).lower()


def parse_manifest(text: str, source: str) -> Manifest:
    """The manifest in `text`; `source` names the module it was read from, for the error."""
    try:
        data = tomllib.loads(text)
        return _manifest(data)
    except (tomllib.TOMLDecodeError, ValueError, InterfaceContractError) as error:
        raise InterfaceContractError(
            "MODULE_MANIFEST_INVALID", f"{source}/module.toml: {error}", {"module": source}
        ) from None


def _keys(
    table: object, where: str, keys: set[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise ValueError(f"{where} must be a table")
    missing = sorted(keys - set(table))
    unknown = sorted(set(table) - keys - optional)
    if missing or unknown:
        raise ValueError(f"{where}: missing {missing}, unknown {unknown}")
    return table


def _strings(
    value: object, where: str, pattern: re.Pattern[str], empty: bool = True
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} must be a list of strings")
    if not empty and not value:
        raise ValueError(f"{where} may not be empty")
    if not all(pattern.fullmatch(item) for item in value):
        raise ValueError(f"{where}: {value} does not match {pattern.pattern}")
    return tuple(value)


def _distributions(value: object, where: str) -> tuple[str, ...]:
    names = [canonical(name) for name in _strings(value, where, DISTRIBUTION)]
    if len(set(names)) != len(names):
        raise ValueError(f"{where} names a distribution twice")
    return tuple(sorted(names))


def _capabilities(value: object, where: str) -> tuple[str, ...]:
    found = _strings(value, where, NAME, empty=False)
    if len(set(found)) != len(found):
        raise ValueError(f"{where} names a capability twice")
    return found


def _contract(entry: dict[str, Any], where: str) -> tuple[str, Version, tuple[str, ...]]:
    contract = entry["contract"]
    if not isinstance(contract, str) or not NAME.fullmatch(contract):
        raise ValueError(f"{where}: contract {contract!r} is not lowercase-kebab-case")
    version = entry["version"]
    if not isinstance(version, str):
        raise ValueError(f"{where}: version must be a string")
    return (
        contract,
        Version.parse(version),
        _capabilities(entry["capabilities"], f"{where}.capabilities"),
    )


def _manifest(data: dict[str, Any]) -> Manifest:
    _keys(data, "the manifest", {"module", "dependencies"}, frozenset({"provides", "requires"}))
    module = data["module"]
    if not isinstance(module, str) or not MODULE.fullmatch(module):
        raise ValueError(f"module {module!r} is not a module name")
    dependencies = _keys(data["dependencies"], "[dependencies]", {"runtime", "tests"})
    provides = []
    for index, entry in enumerate(_tables(data.get("provides", []), "provides")):
        where = f"provides[{index}]"
        _keys(entry, where, {"contract", "version", "capabilities", "implementation"})
        implementation = entry["implementation"]
        if not isinstance(implementation, str) or not IDENTIFIER.fullmatch(implementation):
            raise ValueError(
                f"{where}: implementation is one attribute of the module's api package"
            )
        provides.append(Provided(*_contract(entry, where), implementation))
    requires = []
    for index, entry in enumerate(_tables(data.get("requires", []), "requires")):
        where = f"requires[{index}]"
        _keys(entry, where, {"contract", "version", "capabilities"})
        requires.append(Required(*_contract(entry, where)))
    for kind, entries in (("provides", provides), ("requires", requires)):
        contracts = [entry.contract for entry in entries]
        if len(set(contracts)) != len(contracts):
            raise ValueError(f"{kind} names a contract twice")
    return Manifest(
        module,
        tuple(provides),
        tuple(requires),
        _distributions(dependencies["runtime"], "dependencies.runtime"),
        _distributions(dependencies["tests"], "dependencies.tests"),
    )


def _tables(value: object, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{where} must be an array of tables")
    return value
