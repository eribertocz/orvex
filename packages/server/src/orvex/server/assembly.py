"""The assembly check (#486 G1): before the server composes the modules, every interface contract
a module requires has one chosen implementation, at a compatible version, with every required
capability and every operation of what it claims to provide, each with the contract's exact
signature, envelope included.

Every installed module may ship `module.toml` (`orvex.contracts.manifest`). When several
modules provide one contract, the configuration chooses one (`chosen`: contract name to module,
#486 §2.2); a chosen module outside the product's distributions, such as an alternative
implementation, is read by its module path. An implementation is loaded only from the module's
`api` package, the one place this file imports a module by. Its construction protocol: it is a
class, built from configuration by keyword arguments, so it takes no positional-only or
variadic positional parameter. `UNSUPPORTED_OPERATION` cannot stand in for a capability: the
contract format refuses to declare it, and an operation the implementation lacks refuses the
assembly. Any problem stops the server before it opens a project or binds a port (exit 3).
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from importlib.resources import files

from orvex.contracts.access.fetcher import ProviderFetcher
from orvex.contracts.access.interface import ACCESS
from orvex.contracts.audit_log.interface import AUDIT_LOG
from orvex.contracts.binding import Bound
from orvex.contracts.interface import InterfaceContract
from orvex.contracts.manifest import Manifest, Provided, parse_manifest
from orvex.contracts.registry import interface_contracts
from orvex.server.identity import Identity, load_identity
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
    chosen: Mapping[str, str] | None = None,
) -> list[str]:
    """Every reason the modules cannot be assembled; empty when they can."""
    choices = dict(chosen or {})
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
            choice = choices.get(required.contract)
            if choice is not None:
                offered = [(module, entry) for module, entry in offered if module == choice]
                if not offered:
                    problems.append(
                        f"ASSEMBLY_CHOICE_UNKNOWN: {where}: the chosen {choice} does not provide it"
                    )
                    continue
            if len(offered) > 1:
                names = ", ".join(module for module, _ in offered)
                problems.append(
                    f"ASSEMBLY_AMBIGUOUS_IMPLEMENTATION: {where}: provided by {names}; "
                    "the configuration chooses one"
                )
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
    if not isinstance(implementation, type):
        return [f"ASSEMBLY_CONSTRUCTION_INVALID: {where}: {provided.implementation} is not a class"]
    problems = _construction_problems(where, implementation)
    operations = [
        operation
        for capability in provided.capabilities
        for operation in contract.operations_of(capability)
        if provided.version.provides(contract.since(operation))
    ]
    missing = [o.name for o in operations if not callable(getattr(implementation, o.name, None))]
    if missing:
        problems.append(
            f"ASSEMBLY_OPERATION_MISSING: {where}: {provided.implementation} lacks {missing}"
        )
    for operation in operations:
        if operation.name not in missing:
            mismatch = contract.mismatch(operation.name, implementation)
            if mismatch is not None:
                problems.append(f"ASSEMBLY_SIGNATURE_MISMATCH: {where}: {mismatch}")
    return problems


def _construction_problems(where: str, implementation: type) -> list[str]:
    """The construction protocol: built from configuration by keyword arguments only."""
    try:
        parameters = inspect.signature(implementation).parameters.values()
    except (TypeError, ValueError) as error:
        return [f"ASSEMBLY_CONSTRUCTION_INVALID: {where}: {error}"]
    positional = [
        parameter.name
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL)
    ]
    if positional:
        return [
            f"ASSEMBLY_CONSTRUCTION_INVALID: {where}: {implementation.__qualname__} takes "
            f"{positional} by position; it is built from configuration by keyword"
        ]
    return []


def check_assembly(
    manifests: Sequence[Manifest],
    contracts: Mapping[str, InterfaceContract],
    implementations: Mapping[tuple[str, str], object],
    chosen: Mapping[str, str] | None = None,
) -> None:
    problems = assembly_problems(manifests, contracts, implementations, chosen)
    if problems:
        raise AssemblyRefused(problems)


def installed_manifests(
    identity: Identity, chosen: Mapping[str, str] | None = None
) -> tuple[Manifest, ...]:
    """The `module.toml` of every installed module that ships one, and of each chosen module."""
    modules = {module for module, _ in module_versions(identity)} | set((chosen or {}).values())
    found = []
    problems = []
    for module in sorted(modules):
        try:
            resource = files(f"{identity.python_namespace}.{module}").joinpath("module.toml")
        except ModuleNotFoundError:
            problems.append(f"ASSEMBLY_CHOICE_UNKNOWN: the chosen module {module} is not installed")
            continue
        if not resource.is_file():
            continue
        manifest = parse_manifest(resource.read_text("utf-8"), module)
        if manifest.module != module:
            problems.append(
                f"ASSEMBLY_MANIFEST_MISPLACED: the manifest of {module} names {manifest.module}"
            )
        found.append(manifest)
    if problems:
        raise AssemblyRefused(problems)
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


def check_installed_assembly(identity: Identity, chosen: Mapping[str, str] | None = None) -> None:
    """The check run before the server starts, on the installed modules and the choices."""
    manifests = installed_manifests(identity, chosen)
    contracts_package = importlib.import_module(f"{identity.python_namespace}.contracts")
    check_assembly(
        manifests,
        interface_contracts(contracts_package),
        load_implementations(identity.python_namespace, manifests),
        chosen,
    )


def access_bindings(
    identity: Identity | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    fetcher: ProviderFetcher | None = None,
    new_id: Callable[[], str] | None = None,
) -> tuple[Bound, Bound]:
    """The `access` and `audit-log` contracts, after the whole assembly is checked, on one new
    implementation of both, composed with `clock`, `new_id` (the source of new user ids) and
    the server's provider `fetcher`
    (`orvex.contracts.access.fetcher`), each reached through the in-process binding. Both
    contracts must be provided by the same implementation: the audit log of an instance is the
    one its directory writes to. Without `identity`, the installed product's (`identity.toml`):
    the one entry tests and runners reach access through."""
    if identity is None:
        identity = load_identity()  # the installed product's
    namespace = identity.python_namespace
    manifests = installed_manifests(identity)
    contracts = interface_contracts(importlib.import_module(f"{namespace}.contracts"))
    implementations = load_implementations(namespace, manifests)
    check_assembly(manifests, contracts, implementations)
    chosen = {
        contract: [made for key, made in implementations.items() if key[1] == contract]
        for contract in (ACCESS.name, AUDIT_LOG.name)
    }
    found = {id(made) for each in chosen.values() for made in each}
    if any(len(each) != 1 for each in chosen.values()) or len(found) != 1:
        raise AssemblyRefused(
            [
                "ASSEMBLY_AUDIT_LOG_APART: access and audit-log need one implementation of both, "
                f"found {sum(len(each) for each in chosen.values())}"
            ]
        )
    made = chosen[ACCESS.name][0]
    composition = {"clock": clock, "fetcher": fetcher, "new_id": new_id}
    implementation = made(**composition) if isinstance(made, type) else made
    return (
        Bound(ACCESS, implementation, serialized=False),
        Bound(AUDIT_LOG, implementation, serialized=False),
    )
