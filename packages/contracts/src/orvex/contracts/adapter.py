"""Public test adapters (#486 G4): implementations of an interface contract, shipped with it,
that a module's own tests use in place of the modules that provide the contracts it requires.

An adapter lives in `<package>.<contract>.adapter` (`orvex.contracts.registry`) and must pass
its contract's whole kit (`packages/contracts/tests`), so a module runs its own operations
standalone, with only `orvex-contracts` installed and no module that only consumes it.
`required_adapters` gives a module the adapter of each contract its manifest requires, or
refuses with MODULE_ADAPTER_MISSING when one is missing or at an incompatible version.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .interface import InterfaceContract
from .interface_errors import InterfaceContractError
from .manifest import Manifest


@dataclass(frozen=True, slots=True)
class ContractAdapter:
    """A public test adapter: `make` builds a fresh implementation of `contract` in full."""

    contract: InterfaceContract
    make: Callable[[], object]


def required_adapters(
    manifest: Manifest, adapters: Mapping[str, ContractAdapter]
) -> dict[str, ContractAdapter]:
    """The adapter of every contract `manifest` requires, by contract name."""
    found: dict[str, ContractAdapter] = {}
    missing: list[str] = []
    for required in manifest.requires:
        adapter = adapters.get(required.contract)
        if adapter is None:
            missing.append(f"{required.contract} {required.version}: no public test adapter")
        elif adapter.contract.name != required.contract:
            missing.append(
                f"{required.contract} {required.version}: the adapter found implements "
                f"{adapter.contract.name}"
            )
        elif not adapter.contract.version.provides(required.version):
            missing.append(
                f"{required.contract} {required.version}: the adapter is at "
                f"{adapter.contract.version}"
            )
        else:
            found[required.contract] = adapter
    if missing:
        raise InterfaceContractError(
            "MODULE_ADAPTER_MISSING",
            f"{manifest.module} requires " + "; ".join(missing),
            {"module": manifest.module},
        )
    return found
