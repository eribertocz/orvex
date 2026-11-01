"""The Ossie version registry (ADR-0030 §1): which upstream revisions this engine reads.

Each entry is a folder of `revisions/` holding a `PIN.toml` and the files it hashes; the folder
is a byte-identical copy of `vendor/apache-ossie/` for that revision. A development version
number is not an identity: two revisions with the same `specification_version` are two entries,
and at most one of them is active. Only the files the reader needs at run time are shipped (the
schema); the normative text stays in `vendor/`.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, Literal

State = Literal["stable", "experimental", "read_only", "rejected"]
STATES: tuple[State, ...] = ("stable", "experimental", "read_only", "rejected")
# The states a document may be read under; only the first two compile and execute.
ACTIVE_STATES = frozenset({"stable", "experimental", "read_only"})
EXECUTABLE_STATES = frozenset({"stable", "experimental"})
SHIPPED = ("PIN.toml", "ossie-schema.json")


class RegistryInvalid(ValueError):
    """The registry itself is wrong: a programming or packaging error, never a user's."""


@dataclass(frozen=True)
class Entry:
    id: str
    specification_version: str
    commit: str
    state: State
    reader: str
    capabilities: int
    upstream_dialects: tuple[str, ...]
    product_dialects: tuple[str, ...]
    sha256: Mapping[str, str] = field(compare=False, repr=False)
    schema: Mapping[str, Any] = field(compare=False, repr=False)

    @property
    def executable(self) -> bool:
        return self.state in EXECUTABLE_STATES

    def dialects(self, vendor_names: Iterable[str]) -> frozenset[str]:
        """Every dialect token recognized for this entry: the upstream catalog at its commit,
        this product's own tokens, and the product's vendor names (its own dialect token)."""
        return frozenset((*self.upstream_dialects, *self.product_dialects, *vendor_names))


@dataclass(frozen=True)
class Registry:
    entries: tuple[Entry, ...]

    def __post_init__(self) -> None:
        active: dict[str, str] = {}
        ids: set[str] = set()
        for entry in self.entries:
            if entry.id in ids:
                raise RegistryInvalid(f"two registry entries are named {entry.id!r}")
            ids.add(entry.id)
            if entry.state not in ACTIVE_STATES:
                continue
            other = active.get(entry.specification_version)
            if other is not None:
                raise RegistryInvalid(
                    f"entries {other!r} and {entry.id!r} are both active for "
                    f"{entry.specification_version!r}"
                )
            active[entry.specification_version] = entry.id

    def active(self, specification_version: str) -> Entry | None:
        """The entry that reads documents declaring this version, if any."""
        for entry in self.entries:
            if entry.state in ACTIVE_STATES and entry.specification_version == specification_version:
                return entry
        return None

    def newest_stable(self) -> Entry:
        stable = [entry for entry in self.entries if entry.state == "stable"]
        if not stable:
            raise RegistryInvalid("the registry has no stable entry")
        return stable[-1]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_entry(folder: Traversable) -> Entry:
    """One entry from its folder; every shipped file must match the hash its PIN records."""
    pin: dict[str, Any] = tomllib.loads(folder.joinpath("PIN.toml").read_text(encoding="utf-8"))
    recorded: dict[str, str] = pin["sha256"]
    for name in SHIPPED[1:]:
        content = folder.joinpath(name).read_bytes()
        if recorded.get(name) != _sha256(content):
            raise RegistryInvalid(f"{folder.name}/{name} does not match its recorded SHA-256")
    state = pin["state"]
    if state not in STATES:
        raise RegistryInvalid(f"{folder.name}: unknown state {state!r}")
    schema = json.loads(folder.joinpath("ossie-schema.json").read_text(encoding="utf-8"))
    if schema["properties"]["version"].get("const") != pin["specification_version"]:
        raise RegistryInvalid(f"{folder.name}: the schema reads another version")
    return Entry(
        id=str(pin["id"]),
        specification_version=str(pin["specification_version"]),
        commit=str(pin["commit"]),
        state=state,
        reader=str(pin["reader"]),
        capabilities=int(pin["capabilities"]),
        upstream_dialects=tuple(pin["upstream_dialects"]),
        product_dialects=tuple(pin["product_dialects"]),
        sha256=dict(recorded),
        schema=schema,
    )


@cache
def default_registry() -> Registry:
    """The registry shipped with this package."""
    revisions = files("orvex.semantic.ossie").joinpath("revisions")
    folders = sorted(
        (item for item in revisions.iterdir() if item.is_dir()), key=lambda item: item.name
    )
    return Registry(tuple(load_entry(folder) for folder in folders))
