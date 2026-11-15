"""The implementations the server is assembled with (#486 G1, §2.2): which module provides
each contract, by configuration only, and the chosen implementation of one contract.

`<SLUG>_IMPLEMENTATION_<CONTRACT>` (the contract's name in upper case, `-` as `_`) names the
module chosen for that contract, an alternative outside the product's distributions included.
A value that is not a module name is refused (ASSEMBLY_CHOICE_INVALID); everything else is
checked by `assembly` before any implementation is handed out.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping

from orvex.contracts.manifest import MODULE
from orvex.contracts.registry import interface_contracts
from orvex.server.assembly import (
    AssemblyRefused,
    check_assembly,
    installed_manifests,
    load_implementations,
)
from orvex.server.identity import Identity


def configured_choices(identity: Identity, environment: Mapping[str, str]) -> dict[str, str]:
    """The module configured for each contract, by contract name."""
    start = f"{identity.slug.upper().replace('-', '_')}_IMPLEMENTATION_"
    found: dict[str, str] = {}
    invalid: list[str] = []
    for variable, module in sorted(environment.items()):
        if not variable.startswith(start):
            continue
        if not MODULE.fullmatch(module):
            invalid.append(f"ASSEMBLY_CHOICE_INVALID: {variable}={module!r} is not a module name")
            continue
        found[variable.removeprefix(start).lower().replace("_", "-")] = module
    if invalid:
        raise AssemblyRefused(invalid)
    return found


def implementation_of(
    identity: Identity, contract: str, environment: Mapping[str, str] | None = None
) -> object:
    """The implementation of `contract` the server is assembled with, once the whole assembly
    is checked with the configured choices."""
    chosen = configured_choices(identity, os.environ if environment is None else environment)
    manifests = installed_manifests(identity, chosen)
    loaded = load_implementations(identity.python_namespace, manifests)
    package = importlib.import_module(f"{identity.python_namespace}.contracts")
    contracts = interface_contracts(package)
    check_assembly(manifests, contracts, loaded, chosen)
    offered = sorted(
        (module, implementation)
        for (module, name), implementation in loaded.items()
        if name == contract and chosen.get(contract, module) == module
    )
    if not offered:
        problem = f"ASSEMBLY_NO_IMPLEMENTATION: no installed module provides {contract}"
        raise AssemblyRefused([problem])
    if len(offered) > 1:
        names = ", ".join(module for module, _ in offered)
        raise AssemblyRefused(
            [f"ASSEMBLY_AMBIGUOUS_IMPLEMENTATION: {contract} is provided by {names}; choose one"]
        )
    return offered[0][1]
