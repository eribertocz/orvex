"""Versions of the running product: modules and contracts installed with it."""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distributions, version

from sextant.server.identity import Identity

_CONTRACT_VERSION = re.compile(r"v[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class Versions:
    product: str
    modules: tuple[tuple[str, str], ...]
    contracts: tuple[tuple[str, str], ...]


def module_versions(identity: Identity) -> tuple[tuple[str, str], ...]:
    """(module, version) for every installed distribution of the product."""
    prefix = f"{identity.slug}-"
    found: dict[str, str] = {}
    for distribution in distributions():
        name = distribution.metadata["Name"].lower().replace("_", "-")
        if name == identity.slug:
            found["server"] = distribution.version
        elif name.startswith(prefix):
            found[name.removeprefix(prefix)] = distribution.version
    return tuple(sorted(found.items()))


def contract_versions(identity: Identity) -> tuple[tuple[str, str], ...]:
    """(contract, vN) for every generated contract module installed with the product."""
    try:
        package = importlib.import_module(f"{identity.python_namespace}.contracts")
    except ModuleNotFoundError:
        return ()
    found: set[tuple[str, str]] = set()
    for contract in pkgutil.iter_modules(package.__path__):
        if not contract.ispkg:
            continue
        module = importlib.import_module(f"{package.__name__}.{contract.name}")
        for generated in pkgutil.iter_modules(module.__path__):
            if _CONTRACT_VERSION.fullmatch(generated.name):
                found.add((contract.name.replace("_", "-"), generated.name))
    return tuple(sorted(found))


def current_versions(identity: Identity) -> Versions:
    try:
        product = version(identity.slug)
    except PackageNotFoundError:
        product = "0.0.0"
    return Versions(product, module_versions(identity), contract_versions(identity))
