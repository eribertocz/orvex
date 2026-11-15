"""The assembly check (#486 G1): before the server composes the modules, every interface contract
a module requires has exactly one installed implementation, at a compatible version, with every
required capability and every operation of what it claims to provide.

Every installed module may ship `module.toml` (`orvex.contracts.manifest`). An implementation
is loaded only from the module's `api` package, the one place this file imports a module by.
`UNSUPPORTED_OPERATION` cannot stand in for a capability: the contract format refuses to
declare it, and an operation the implementation lacks refuses the assembly. Any problem stops
the server before it opens a project or binds a port (exit status 3).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from importlib.resources import files

from orvex.contracts.interface import InterfaceContract
from orvex.contracts.manifest import Manifest, Provided, parse_manifest
from orvex.contracts.registry import interface_contracts
from orvex.server.identity import Identity
from orvex.server.versions import module_versions


class AssemblyRefused(Exception):
    """The chosen implementations cannot serve their consumers; the server must not start."""

    code = "ASSEMBLY_REFUSED"

    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{self.code}: " + "; ".join(problems))
        self.problems = problems


def assembly_problems(
    manifests: Sequence[Manifest],
    contracts: Mapping[str, InterfaceContract],
    implementations: Mapping[tuple[str, str], object],
) -> list[str]:
    """Every reason the modules cannot be assembled; empty when they can."""
    problems: list[str] = []
    providers: dict[str, list[tuple[str, Provided]]] = {}
    for manifest in manifests:
        for provided in manifest.provides:
            providers.setdefault(provided.contract, []).append((manifest.module, provided))
            problems += _claim_problems(manifest.module, provided, contracts, implementations)
    for manifest in manifests:
        for required in manifest.requires:
            where = f"{manifest.module} requires {required.contract} {required.version}"
            contract = contracts.get(required.contract)
            if contract is None:
                problems.append(f"ASSEMBLY_CONTRACT_UNKNOWN: {where}: no such interface contract")
                continue
            unknown = sorted(set(required.capabilities) - set(contract.capabilities))
            if unknown:
                problems.append(
                    f"ASSEMBLY_CAPABILITY_UNKNOWN: {where}: {unknown} are not its capabilities"
                )
            offered = providers.get(required.contract, [])
            if not offered:
                problems.append(
                    f"ASSEMBLY_NO_IMPLEMENTATION: {where}: no installed module provides it"
                )
                continue
            if len(offered) > 1:
                names = ", ".join(module for module, _ in offered)
                problems.append(f"ASSEMBLY_AMBIGUOUS_IMPLEMENTATION: {where}: provided by {names}")
                continue
            module, provided = offered[0]
            if not provided.version.provides(required.version):
                problems.append(
                    f"ASSEMBLY_VERSION_INCOMPATIBLE: {where}: {module} provides {provided.version}"
                )
            missing = sorted(set(required.capabilities) - set(provided.capabilities) - set(unknown))
            if missing:
                problems.append(
                    f"ASSEMBLY_CAPABILITY_MISSING: {where}: {module} does not provide {missing}"
                )
    return problems


def _claim_problems(
    module: str,
    provided: Provided,
    contracts: Mapping[str, InterfaceContract],
    implementations: Mapping[tuple[str, str], object],
) -> list[str]:
    """What is untrue in one module's claim to provide a contract."""
    where = f"{module} provides {provided.contract} {provided.version}"
    contract = contracts.get(provided.contract)
    if contract is None:
        return [f"ASSEMBLY_CONTRACT_UNKNOWN: {where}: no such interface contract"]
    if not contract.version.provides(provided.version):
        return [
            f"ASSEMBLY_VERSION_INCOMPATIBLE: {where}: the installed contract is {contract.version}"
        ]
    unknown = sorted(set(provided.capabilities) - set(contract.capabilities))
    if unknown:
        return [f"ASSEMBLY_CAPABILITY_UNKNOWN: {where}: {unknown} are not its capabilities"]
    implementation = implementations.get((module, provided.contract))
    missing = [
        operation.name
        for capability in provided.capabilities
        for operation in contract.operations_of(capability)
        if not callable(getattr(implementation, operation.name, None))
    ]
    if missing:
        return [f"ASSEMBLY_OPERATION_MISSING: {where}: {provided.implementation} lacks {missing}"]
    return []


def check_assembly(
    manifests: Sequence[Manifest],
    contracts: Mapping[str, InterfaceContract],
    implementations: Mapping[tuple[str, str], object],
) -> None:
    problems = assembly_problems(manifests, contracts, implementations)
    if problems:
        raise AssemblyRefused(problems)


def installed_manifests(identity: Identity) -> tuple[Manifest, ...]:
    """The `module.toml` of every installed module that ships one."""
    found = []
    for module, _ in module_versions(identity):
        resource = files(f"{identity.python_namespace}.{module}").joinpath("module.toml")
        if resource.is_file():
            found.append(parse_manifest(resource.read_text("utf-8"), module))
    return tuple(found)


def load_implementations(
    namespace: str, manifests: Sequence[Manifest]
) -> dict[tuple[str, str], object]:
    """Each provided implementation, taken from its module's `api` package and nowhere else."""
    loaded: dict[tuple[str, str], object] = {}
    problems = []
    for manifest in manifests:
        for provided in manifest.provides:
            package = f"{namespace}.{manifest.module}.api"
            try:
                implementation = getattr(importlib.import_module(package), provided.implementation)
            except (ModuleNotFoundError, AttributeError) as error:
                problems.append(
                    f"ASSEMBLY_IMPLEMENTATION_MISSING: {manifest.module} provides "
                    f"{provided.contract} as {package}.{provided.implementation}: {error}"
                )
                continue
            loaded[(manifest.module, provided.contract)] = implementation
    if problems:
        raise AssemblyRefused(problems)
    return loaded


def check_installed_assembly(identity: Identity) -> None:
    """The check run before the server starts, on the installed modules."""
    manifests = installed_manifests(identity)
    contracts_package = importlib.import_module(f"{identity.python_namespace}.contracts")
    check_assembly(
        manifests,
        interface_contracts(contracts_package),
        load_implementations(identity.python_namespace, manifests),
    )
