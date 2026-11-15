"""A small interface contract whose kit cases declare their own environment, and its
implementation, which reads only that environment, for the tests of the case process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.kit import CaseEnvironment, ExtensionDirectory, Kit, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.version import Version

ENGINE = "STATION_ENGINE_VERSION"
EXTENSIONS = "STATION_EXTENSIONS"
SOURCE = "STATION_EXTENSION_SOURCE"


class StationError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class Station(Protocol):
    def setting(self, envelope: CallEnvelope, name: str) -> str | None: ...

    def extensions(self, envelope: CallEnvelope) -> tuple[str, ...]: ...


STATION = InterfaceContract(
    name="station",
    version=Version(1, 0),
    protocol=Station,
    error_type=StationError,
    capabilities=("read",),
    errors=(),
    operations=(Operation("setting", "read"), Operation("extensions", "read")),
)


class EnvironmentStation:
    """Answers from its process's environment and nothing else."""

    def setting(self, envelope: CallEnvelope, name: str) -> str | None:
        return os.environ.get(name)

    def extensions(self, envelope: CallEnvelope) -> tuple[str, ...]:
        directory = os.environ.get(EXTENSIONS)
        if directory is None:
            return ()
        return tuple(sorted(p.name for p in Path(directory).rglob("*") if p.is_file()))


def _expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def declared_engine(station: KitClient) -> None:
    _expect(station.setting(ENGINE) == "26.10", "the declared engine version is not seen")
    _expect(station.setting(SOURCE) is None, "the case inherited the kit's environment")


def without_httpfs(station: KitClient) -> None:
    found = station.extensions()
    _expect(found == ("json.duckdb_extension",), f"extensions {found}")


def empty_directory(station: KitClient) -> None:
    _expect(station.extensions() == (), "an empty extension directory holds files")


STATION_KIT = Kit(
    contract=STATION,
    recorded=3,
    cases=(
        KitCase(
            "setting.declared-engine",
            "setting",
            "the engine version the case declares is the one the implementation sees",
            declared_engine,
            environment=CaseEnvironment(variables=((ENGINE, "26.10"),)),
        ),
        KitCase(
            "extensions.without-httpfs",
            "extensions",
            "an extension directory copied without httpfs lacks it",
            without_httpfs,
            environment=CaseEnvironment(
                extensions=ExtensionDirectory(EXTENSIONS, source=SOURCE, without=("httpfs",))
            ),
        ),
        KitCase(
            "extensions.empty",
            "extensions",
            "an empty extension directory holds no extension",
            empty_directory,
            environment=CaseEnvironment(extensions=ExtensionDirectory(EXTENSIONS)),
        ),
    ),
)
