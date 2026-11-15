"""The interface contracts, kits and public test adapters a contracts package ships, found by
their place: `<package>.<name>.interface` holds `InterfaceContract` values,
`<package>.<name>.kit` holds `Kit` values and `<package>.<name>.adapter` holds
`ContractAdapter` values (ADR-0033)."""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from types import ModuleType

from .adapter import ContractAdapter
from .interface import InterfaceContract
from .interface_errors import InterfaceContractError
from .kit import Kit


def interface_contracts(package: ModuleType) -> dict[str, InterfaceContract]:
    return _declared(package, "interface", InterfaceContract)


def kits(package: ModuleType) -> dict[str, Kit]:
    return _declared(package, "kit", Kit)


def adapters(package: ModuleType) -> dict[str, ContractAdapter]:
    return _declared(package, "adapter", ContractAdapter)


def _name(value: InterfaceContract | Kit | ContractAdapter) -> str:
    return value.name if isinstance(value, InterfaceContract) else value.contract.name


def _declared[Declared: (InterfaceContract, Kit, ContractAdapter)](
    package: ModuleType, place: str, kind: type[Declared]
) -> dict[str, Declared]:
    found: dict[str, Declared] = {}
    for entry in sorted(pkgutil.iter_modules(package.__path__), key=lambda e: e.name):
        name = f"{package.__name__}.{entry.name}.{place}"
        if not entry.ispkg or importlib.util.find_spec(name) is None:
            continue
        for value in vars(importlib.import_module(name)).values():
            if not isinstance(value, kind):
                continue
            key = _name(value)
            if key in found and found[key] is not value:
                raise InterfaceContractError(
                    "INTERFACE_CONTRACT_INVALID",
                    f"two {place} declarations name the contract {key!r}",
                    {"contract": key},
                )
            found[key] = value
    return found
