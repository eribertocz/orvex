"""Versions of the running product, and the contract compatibility check run before serving.

Every installed module ships `contracts.toml`, declaring the contract versions it produces and
the versions it accepts for each contract it consumes (docs/process/versioning.md §3).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib.metadata import distributions
from importlib.resources import files

from orvex.server.identity import Identity

CONTRACT_VERSION = re.compile(r"v[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class Declaration:
    """What one module produces ({contract: version}) and consumes ({contract: versions})."""

    module: str
    produces: tuple[tuple[str, str], ...]
    consumes: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class Versions:
    product: str
    modules: tuple[tuple[str, str], ...]
    contracts: tuple[tuple[str, str], ...]


class ContractVersionIncompatible(Exception):
    """The installed modules cannot work together; the server must not start."""

    code = "CONTRACT_VERSION_INCOMPATIBLE"

    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{self.code}: " + "; ".join(problems))
        self.problems = problems


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


def parse_declaration(module: str, text: str) -> Declaration:
    data = tomllib.loads(text)
    produces = data.get("produces", {})
    consumes = data.get("consumes", {})
    if not isinstance(produces, dict) or not isinstance(consumes, dict):
        raise ValueError(f"{module}: contracts.toml needs tables `produces` and `consumes`")
    for contract, version in produces.items():
        if not isinstance(version, str) or not CONTRACT_VERSION.fullmatch(version):
            raise ValueError(f"{module}: produces {contract} {version!r}, expected vN")
    for contract, accepted in consumes.items():
        if (
            not isinstance(accepted, list)
            or not accepted
            or not all(isinstance(v, str) and CONTRACT_VERSION.fullmatch(v) for v in accepted)
        ):
            raise ValueError(f"{module}: consumes {contract} {accepted!r}, expected [vN, ...]")
    return Declaration(
        module=module,
        produces=tuple(sorted(produces.items())),
        consumes=tuple(sorted((name, tuple(sorted(v))) for name, v in consumes.items())),
    )


def installed_declarations(identity: Identity) -> tuple[Declaration, ...]:
    """The `contracts.toml` of every installed module; a module without one declares nothing."""
    found = []
    for module, _ in module_versions(identity):
        resource = files(f"{identity.python_namespace}.{module}").joinpath("contracts.toml")
        text = resource.read_text("utf-8") if resource.is_file() else ""
        found.append(parse_declaration(module, text))
    return tuple(found)


def check_compatibility(declarations: tuple[Declaration, ...]) -> None:
    """Every consumed contract has exactly one producer offering an accepted version."""
    producers: dict[str, list[tuple[str, str]]] = {}
    for declaration in declarations:
        for contract, version in declaration.produces:
            producers.setdefault(contract, []).append((declaration.module, version))
    problems = []
    for contract, offered in sorted(producers.items()):
        if len(offered) > 1:
            names = ", ".join(f"{module} {version}" for module, version in offered)
            problems.append(f"contract {contract} has more than one producer ({names})")
    for declaration in declarations:
        for contract, accepted in declaration.consumes:
            offered = producers.get(contract, [])
            wanted = ", ".join(accepted)
            if not offered:
                problems.append(
                    f"contract {contract}: no installed module produces it "
                    f"(consumer {declaration.module} accepts {wanted})"
                )
            elif offered[0][1] not in accepted:
                producer, version = offered[0]
                problems.append(
                    f"contract {contract}: producer {producer} offers {version}, "
                    f"consumer {declaration.module} accepts {wanted}"
                )
    if problems:
        raise ContractVersionIncompatible(problems)


def product_version(identity: Identity) -> str:
    manifest = files(f"{identity.python_namespace}.server").joinpath("versions.toml")
    product: str = tomllib.loads(manifest.read_text("utf-8"))["product"]
    return product


def current_versions(
    identity: Identity, declarations: tuple[Declaration, ...] | None = None
) -> Versions:
    """The product version, installed module versions and produced contract versions."""
    declared = installed_declarations(identity) if declarations is None else declarations
    contracts = sorted({item for declaration in declared for item in declaration.produces})
    return Versions(product_version(identity), module_versions(identity), tuple(contracts))
