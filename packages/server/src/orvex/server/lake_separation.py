"""The instance's protected set, and every existing DuckLake kept apart from it (P2-30 PR 2,
#399; ADR-0027 §3, "Separate locations").

An existing lake's catalog decides which files it reads, so no lake may reach a file Orvex reads
for any other purpose, in any project of the instance. The **protected set** holds:

- the instance folder (and its project registry);
- for every registered project: its folder; its managed lake's data location and catalog file;
  its existing lakes' data locations and SQLite catalog files; and every path its file sources
  read (a glob by the folder before its first wildcard).

The check runs both ways, across the instance, whenever a project's configuration is validated:
a lake location that overlaps a protected location is refused, and so is a protected location
declared later inside a lake location, in whichever order the projects were registered. The
rules themselves are the store's (`orvex.store.separation`): whole path components after
resolving, object storage by bucket and key, and no file system shared with a local data
location or a SQLite catalog. Connections with the same catalog identity (host, port, database
and schema, or one SQLite file by device and inode) and the same data location are one lake
connected twice, never an overlap.

`protection` gives the store what an attachment needs: the roles in use elsewhere and the
protected files and locations, built from the same set. Nothing is opened but the project files,
and nothing is written.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orvex.contracts.project.v1 import (
    CsvConnection,
    DuckdbConnection,
    DucklakeConnection,
    DucklakePostgresCatalog,
    ParquetConnection,
    PostgresConnection,
    PostgresLakeCatalog,
    SqliteConnection,
)
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    ProtectedFile,
    ProtectedLocation,
    RolesInUseItem,
    SqliteLakeLocation,
)
from orvex.server import lake_place
from orvex.server.instance import REGISTRY_FILE, ProjectRegistry
from orvex.server.problems import Problem
from orvex.server.project import CONNECTIONS_FILE, PROJECT_FILE, ProjectLoadError, load_project
from orvex.store import separation
from orvex.store.errors import StoreError

CODE = "STORE_SOURCE_INVALID"
_GLOB_MARK = re.compile(r"[*?\[{]")


@dataclass(frozen=True, slots=True)
class Lake:
    """One existing-lake connection of a project, and the identity that makes two connections
    one lake."""

    folder: Path
    name: str
    data: str
    catalog_file: str | None
    identity: tuple[object, ...]
    role: str | None


@dataclass(frozen=True, slots=True)
class Place:
    """One location of the protected set: where it is declared (`folder` None for the
    instance), what it is (for messages), and the lake it belongs to, if any."""

    folder: Path | None
    file: str
    what: str
    location: str
    is_file: bool
    lake: Lake | None = None


@dataclass(frozen=True, slots=True)
class Instance:
    places: tuple[Place, ...]
    lakes: tuple[Lake, ...]
    roles: tuple[tuple[Path, str | None, str], ...]
    unreadable: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class Protection:
    """An existing lake's inputs to the store's attachment (`ExistingLakeAttachment`)."""

    roles_in_use: list[RolesInUseItem]
    protected_files: list[ProtectedFile]
    protected_locations: list[ProtectedLocation]


def registered(directory: Path, folder: Path) -> list[Path]:
    """The folders of every project registered in the instance, with `folder` among them."""
    folders = [project.path for project in ProjectRegistry(directory).projects()]
    resolved = folder.resolve()
    return folders if resolved in folders else [*folders, resolved]


def instance(directory: Path, folders: Iterable[Path], slug: str) -> Instance:
    """The instance's protected set and existing lakes, from every project's configuration. A
    project that cannot be loaded keeps only its folder in the set and is listed `unreadable`."""
    places = [
        Place(None, ".", "the instance folder", str(directory.resolve()), is_file=False),
        Place(
            None,
            ".",
            "the instance's project registry",
            str(directory.resolve() / REGISTRY_FILE),
            is_file=True,
        ),
    ]
    lakes: list[Lake] = []
    roles: list[tuple[Path, str | None, str]] = []
    unreadable: list[Path] = []
    for folder in dict.fromkeys(Path(item).resolve() for item in folders):
        places.append(Place(folder, PROJECT_FILE, "the project folder", str(folder), False))
        try:
            project = load_project(folder)
            chosen = lake_place.place(project, folder / f".{slug}")
        except ProjectLoadError:
            unreadable.append(folder)
            continue
        places += _managed(folder, chosen.location)
        lake_settings = project.settings.lake
        catalog = lake_settings.catalog if lake_settings is not None else None
        managed = catalog.root if catalog is not None else None
        if isinstance(managed, PostgresLakeCatalog):
            roles.append((folder, None, managed.user))
        for name, connection in project.connections.items():
            what = f"the files of connection {name}"
            if isinstance(connection, ParquetConnection | CsvConnection):
                places.append(Place(folder, CONNECTIONS_FILE, what, _read_root(connection), False))
            elif isinstance(connection, DuckdbConnection | SqliteConnection):
                places.append(Place(folder, CONNECTIONS_FILE, what, connection.path.root, True))
            elif isinstance(connection, PostgresConnection):
                roles.append((folder, name, connection.user))
            elif isinstance(connection, DucklakeConnection):
                lake = _lake(folder, name, connection)
                lakes.append(lake)
                if lake.role is not None:
                    roles.append((folder, name, lake.role))
                places.append(
                    Place(folder, CONNECTIONS_FILE, what, lake.data, is_file=False, lake=lake)
                )
                if lake.catalog_file is not None:
                    places.append(
                        Place(
                            folder,
                            CONNECTIONS_FILE,
                            f"the catalog of connection {name}",
                            lake.catalog_file,
                            is_file=True,
                            lake=lake,
                        )
                    )
    return Instance(tuple(places), tuple(lakes), tuple(roles), tuple(unreadable))


def problems(found: Instance, folder: Path) -> list[Problem]:
    """Every overlap that involves the project at `folder`, on either side: one problem for each
    of its lakes that is not apart from the protected set, and one for each other project's lake
    that one of its protected locations overlaps. Overlapping locations are reported before a
    shared file system."""
    folder = folder.resolve()
    reported: dict[tuple[Path, str], Problem] = {}
    for lake in found.lakes:
        key = (lake.folder, lake.name)
        if lake.folder == folder and found.unreadable:
            reported[key] = Problem(
                CODE,
                CONNECTIONS_FILE,
                None,
                f"Connection {lake.name}: another project of this instance cannot be read, so "
                "the lake cannot be shown apart from its files.",
            )
            continue
        # Another project's lake against this project's places: its own lakes' locations aside,
        # which the loop over this project's lakes compares.
        places = [
            place
            for place in found.places
            if (lake.folder == folder or (place.folder == folder and place.lake is None))
            and place.lake is not lake
            and not (place.lake is not None and _one_lake(lake, place.lake))
        ]
        # A shared file system is the lake's own project's to fix: another project's lake is
        # compared with this project's places by location only.
        for devices in (False, True) if lake.folder == folder else (False,):
            for place in places:
                reason = _reason(lake, place, devices=devices)
                if reason is not None and key not in reported:
                    reported[key] = _problem(lake, place, folder, reason)
    return list(reported.values())


def protection(found: Instance, folder: Path, name: str) -> Protection:
    """The roles in use elsewhere and the protected files and locations for the lake `name` of
    the project at `folder`. Refused (`STORE_SOURCE_INVALID`) while a project is unreadable."""
    folder = folder.resolve()
    lake = next(item for item in found.lakes if item.folder == folder and item.name == name)
    if found.unreadable:
        raise separation.refused(name, "another project of this instance cannot be read")
    places = [p for p in found.places if p.lake is None or not _one_lake(lake, p.lake)]
    roles = sorted({role for where, which, role in found.roles if (where, which) != (folder, name)})
    return Protection(
        roles_in_use=[RolesInUseItem(role) for role in roles],
        protected_files=[ProtectedFile(p.location) for p in places if p.is_file],
        protected_locations=[ProtectedLocation(p.location) for p in places],
    )


def _managed(folder: Path, location: lake_place.Location) -> list[Place]:
    """The managed lake's data location and catalog file."""
    catalog: str | None = None
    if isinstance(location, LocalLakeLocation):
        catalog = location.catalog_path or str(Path(location.path) / "catalog.ducklake")
        data = location.data_path or str(Path(location.path) / "data")
    else:
        data = location.data_path
        if isinstance(location, SqliteLakeLocation):
            catalog = location.path
    found = [Place(folder, PROJECT_FILE, "the managed lake's data location", data, False)]
    if catalog is not None:
        found.append(Place(folder, PROJECT_FILE, "the managed lake's catalog", catalog, True))
    return found


def _read_root(connection: ParquetConnection | CsvConnection) -> str:
    """What a file source reads: its file, or a glob's folder before the first wildcard."""
    path = connection.path.root
    glob = _GLOB_MARK.search(path)
    return path if glob is None else (path[: glob.start()].rsplit("/", 1)[0] or "/")


def _lake(folder: Path, name: str, connection: DucklakeConnection) -> Lake:
    catalog = connection.catalog.root
    data = connection.data_path
    if isinstance(catalog, DucklakePostgresCatalog):
        identity: tuple[object, ...] = (
            "postgres",
            catalog.host.lower(),
            catalog.port or 5432,
            catalog.database,
            catalog.schema_ or "public",
        )
        catalog_file, role = None, catalog.user
    else:
        catalog_file, role = catalog.path.root, None
        try:
            status = os.stat(catalog_file)
            identity = ("sqlite", status.st_dev, status.st_ino)
        except OSError:
            identity = ("sqlite", "unidentified", folder, name)  # never the same as another
    return Lake(folder, name, data, catalog_file, (*identity, *_store(connection)), role)


def _store(connection: DucklakeConnection) -> tuple[str, ...]:
    """The object-storage service of the data location, normalized as the managed lake's."""
    if connection.s3 is not None:
        return ("s3", lake_place._normalized_endpoint(connection.s3.endpoint or ""))
    if connection.gcs is not None:
        return ("gcs", lake_place.GCS_ENDPOINT)
    if connection.azure is not None:
        # The connection string is a secret: two references are one service only when equal.
        return ("azure", connection.azure.connection_string.root)
    return ("local",)


def _one_lake(first: Lake, second: Lake) -> bool:
    """The same lake: same catalog identity and service, and the same data location."""
    if first.identity != second.identity:
        return False
    try:
        return separation.components(first.name, first.data) == separation.components(
            second.name, second.data
        )
    except StoreError:
        return False


def _reason(lake: Lake, place: Place, *, devices: bool) -> str | None:
    """Why `lake` is not apart from `place`: by location, or (`devices`) by file system."""
    try:
        if not devices:
            for location in filter(None, (lake.data, lake.catalog_file)):
                if separation.overlaps(lake.name, location, place.location):
                    return "it overlaps a protected location"
            return None
        files = [place.location] if place.is_file else []
        separation.check_apart(lake.name, lake.data, lake.catalog_file, [place.location], files)
    except StoreError as error:
        return str(error.details.get("reason", error.message))
    return None


def _problem(lake: Lake, place: Place, folder: Path, reason: str) -> Problem:
    if lake.folder == folder:
        against = place.what if place.folder in {folder, None} else "another project's files"
        return Problem(
            CODE,
            CONNECTIONS_FILE,
            None,
            f"Connection {lake.name}: the existing lake is not apart from {against}: {reason}.",
        )
    return Problem(
        CODE,
        place.file,
        None,
        f"{place.what[0].upper()}{place.what[1:]} is not apart from an existing lake of another "
        f"project of this instance: {reason}.",
    )
