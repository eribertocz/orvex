"""Where a project's lake lives (#120, ADR-0027 §2).

`project.yaml`'s optional `lake.catalog` chooses the catalog: a DuckDB file (the default), a
SQLite file, or PostgreSQL. This module turns it into the store's location, refuses what
ADR-0027 does not allow before anything is opened or created, and fixes the choice when the
lake is created: `.<slug>/lake.location` records the catalog's identity, and a project whose
configuration no longer matches it is refused instead of silently getting a second, empty lake.

Data files stay in `.<slug>/lake/data/` (#126 adds their location).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from orvex.contracts.project.v1 import (
    DuckdbLakeCatalog,
    PostgresLakeCatalog,
    SecretReference,
    SqliteLakeCatalog,
)
from orvex.contracts.store.v1 import LocalLakeLocation, PostgresLakeLocation, SqliteLakeLocation
from orvex.server.project import PROJECT_FILE, Project, ProjectLoadError

LAKE_FOLDER = "lake"
RECORD_FILE = "lake.location"
DEFAULT_CATALOG = {"duckdb": "catalog.ducklake", "sqlite": "catalog.sqlite"}
MOUNTINFO = Path("/proc/self/mountinfo")
# File systems where file locking cannot be trusted, or that are remote: a DuckDB or SQLite
# catalog there could be corrupted by two writers (ADR-0027 §2). Every FUSE file system
# (`fuse.sshfs`, `fuse.s3fs`, …) is refused too: most are remote, and none can be proven local.
NETWORK_FILE_SYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "afs",
        # Also remote or shared with another system: WebDAV, and folders a virtual machine
        # shares with its host.
        "fuse",
        "davfs",
        "vboxsf",
        "virtiofs",
    }
)
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_VARIABLE = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")

type Location = LocalLakeLocation | SqliteLakeLocation | PostgresLakeLocation


@dataclass(frozen=True, slots=True)
class Place:
    """The store location of a project's lake, and the identity recorded when it is created:
    the catalog's type, then its resolved path, or host, port, database and schema."""

    location: Location
    identity: dict[str, str | int]


def lake_folder(home: Path) -> Path:
    return home / LAKE_FOLDER


def place(project: Project, home: Path) -> Place:
    """The lake's location from `project.yaml`; `LAKE_LOCATION_INVALID`, `LAKE_TLS_REQUIRED`
    or `PROJECT_SECRET_MISSING` for one that must be refused. Nothing is opened or created."""
    lake = project.settings.lake
    catalog = lake.catalog.root if lake is not None and lake.catalog is not None else None
    if catalog is None:
        # No `lake` section: exactly as before #120.
        return Place(
            LocalLakeLocation(kind="lake_location", catalog="local", path=str(lake_folder(home))),
            _default_identity(home),
        )
    # Paths are compared and recorded resolved (symlinks and `..` followed), so the same file
    # has one identity however the project folder was named.
    folder = lake_folder(home).resolve()
    data = (folder / "data").as_posix() + "/"
    if isinstance(catalog, PostgresLakeCatalog):
        return _postgres(catalog, data)
    kind = "duckdb" if isinstance(catalog, DuckdbLakeCatalog) else "sqlite"
    default = folder / DEFAULT_CATALOG[kind]
    path = _catalog_file(catalog, project.folder, default)
    identity: dict[str, str | int] = {"catalog": kind, "path": path.as_posix()}
    if isinstance(catalog, SqliteLakeCatalog):
        return Place(
            SqliteLakeLocation(
                kind="lake_location", catalog="sqlite", path=str(path), data_path=data
            ),
            identity,
        )
    return Place(
        LocalLakeLocation(
            kind="lake_location",
            catalog="local",
            path=str(folder),
            catalog_path=None if path == default else str(path),
        ),
        identity,
    )


def _invalid(message: str, pointer: str) -> ProjectLoadError:
    return ProjectLoadError("LAKE_LOCATION_INVALID", message, file=PROJECT_FILE, path=pointer)


def _catalog_file(
    catalog: DuckdbLakeCatalog | SqliteLakeCatalog, folder: Path, default: Path
) -> Path:
    """The catalog file's absolute path, refused unless it is on a local disk."""
    pointer = "/lake/catalog/path"
    if catalog.path is None:
        path = default
    else:
        text = catalog.path.root
        if _URL.match(text):
            raise _invalid(
                f"lake.catalog.path {text}: a {catalog.type} catalog is a file on a local disk, "
                "never in object storage; only a postgres catalog can be remote.",
                pointer,
            )
        expanded = Path(os.path.expanduser(text))
        path = (expanded if expanded.is_absolute() else folder / expanded).resolve()
    kind = file_system(path)
    if kind is None:
        if path != default:
            raise _invalid(
                f"lake.catalog.path {path}: this platform cannot tell whether the folder is on "
                "a local disk, so only the default catalog path can be used here.",
                pointer,
            )
    elif kind in NETWORK_FILE_SYSTEMS or kind.startswith("fuse."):
        raise _invalid(
            f"lake.catalog.path {path}: it is on a {kind} file system. A {catalog.type} catalog "
            "must be on a local disk, where file locking is reliable; use a local folder, or a "
            "postgres catalog for a lake shared between machines.",
            pointer,
        )
    return path


def file_system(path: Path) -> str | None:
    """The type of the file system holding `path` (its deepest existing folder), from Linux's
    `/proc/self/mountinfo`; None where that cannot be read (other platforms)."""
    try:
        mountinfo = MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        return mount_type(existing.resolve(), mountinfo)
    except OSError:
        return None


def mount_type(target: Path, mountinfo: str) -> str | None:
    """The file-system type of the deepest mount point holding `target`, in mountinfo's text;
    a later mount on the same point hides an earlier one."""
    best: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split(" ")
        kind = after.split(" ")[0] if separator else ""
        if len(fields) < 5 or not kind:
            continue
        point = Path(_unescape(fields[4]))
        if target == point or point in target.parents:
            depth = len(point.parts)
            if best is None or depth >= best[0]:
                best = (depth, kind)
    return best[1] if best is not None else None


def _unescape(text: str) -> str:
    """mountinfo writes a space, tab, newline and backslash in a path as octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def _postgres(catalog: PostgresLakeCatalog, data: str) -> Place:
    host = catalog.host
    local = host.startswith("/") or host.lower() in _LOCAL_HOSTS
    if not local and catalog.ssl_mode != "verify-full":
        mode = catalog.ssl_mode or "not set"
        raise ProjectLoadError(
            "LAKE_TLS_REQUIRED",
            f"lake.catalog: a PostgreSQL catalog on {host} must use ssl_mode: verify-full "
            f"(found: {mode}), so the connection is encrypted and the server's certificate is "
            "checked against its name. require and verify-ca do not check the name.",
            file=PROJECT_FILE,
            path="/lake/catalog/ssl_mode",
        )
    password = _variable(catalog.password, "/lake/catalog/password")
    pairs = {
        "host": host,
        "port": str(catalog.port or 5432),
        "dbname": catalog.database,
        "user": catalog.user,
    }
    if catalog.ssl_mode is not None:
        pairs["sslmode"] = catalog.ssl_mode
    if catalog.ssl_mode == "verify-full":
        root = catalog.ssl_root_cert or "system"
        if root != "system":
            variable = _variable(SecretReference(root), "/lake/catalog/ssl_root_cert")
            authority = os.environ[variable]
            if not Path(authority).is_file():
                raise _invalid(
                    f"lake.catalog.ssl_root_cert: {variable} does not name a readable "
                    "certificate file.",
                    "/lake/catalog/ssl_root_cert",
                )
            pairs["sslrootcert"] = authority
        else:
            pairs["sslrootcert"] = "system"
    connection = " ".join(f"{key}={_libpq(value)}" for key, value in pairs.items())
    schema = catalog.schema_
    return Place(
        PostgresLakeLocation(
            kind="lake_location",
            catalog="postgres",
            connection=connection,
            password_env=password,
            metadata_schema=schema,
            data_path=data,
        ),
        {
            "catalog": "postgres",
            "host": host,
            "port": catalog.port or 5432,
            "database": catalog.database,
            "schema": schema or "public",
        },
    )


def _variable(reference: SecretReference, pointer: str) -> str:
    """The variable a `${env:NAME}` reference names, which must be set (its value is not read
    here)."""
    match = _VARIABLE.fullmatch(reference.root)
    assert match is not None  # the contract's pattern
    variable = match.group(1)
    if os.environ.get(variable) is None:
        raise ProjectLoadError(
            "PROJECT_SECRET_MISSING",
            f"Environment variable {variable} is not set (lake.catalog).",
            file=PROJECT_FILE,
            path=pointer,
            variable=variable,
        )
    return variable


def _libpq(value: str) -> str:
    """A libpq connection-string value, always quoted: no value can add a keyword."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def recorded(home: Path) -> dict[str, str | int] | None:
    """The identity of the project's existing lake, or None when it has none yet. A lake made
    before #120 has no record: its catalog is the default DuckDB file."""
    record = home / RECORD_FILE
    try:
        found = json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists():
            return _default_identity(home)
        return None
    except (OSError, ValueError) as error:
        raise ProjectLoadError(
            "LAKE_LOCATION_CHANGED",
            f"{RECORD_FILE} cannot be read ({type(error).__name__}); it records where this "
            "project's lake is, so nothing is opened until it is restored.",
            file=f"{home.name}/{RECORD_FILE}",
        ) from None
    if not isinstance(found, dict):
        raise ProjectLoadError(
            "LAKE_LOCATION_CHANGED",
            f"{RECORD_FILE} is not a lake record; nothing is opened until it is restored.",
            file=f"{home.name}/{RECORD_FILE}",
        )
    return found


def exists(home: Path) -> bool:
    """Whether the project has a lake: a record, or the default catalog file."""
    return (home / RECORD_FILE).exists() or (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists()


def check_unchanged(home: Path, chosen: Place) -> None:
    """Refuse a configuration whose catalog is not the one the existing lake was created with."""
    existing = recorded(home)
    if existing is None or existing == chosen.identity:
        return
    raise ProjectLoadError(
        "LAKE_LOCATION_CHANGED",
        f"lake.catalog: this project's lake was created with {_describe(existing)}, but "
        f"project.yaml now names {_describe(chosen.identity)}. A lake is not moved; restore "
        "the previous lake.catalog to use it (nothing was opened or created).",
        file=PROJECT_FILE,
        path="/lake",
    )


def _describe(identity: dict[str, str | int]) -> str:
    if identity.get("catalog") == "postgres":
        return (
            f"a postgres catalog at {identity.get('host')}:{identity.get('port')}, database "
            f"{identity.get('database')}, schema {identity.get('schema')}"
        )
    return f"a {identity.get('catalog')} catalog at {identity.get('path')}"


def _default_identity(home: Path) -> dict[str, str | int]:
    """The identity of the default DuckDB catalog, its path resolved."""
    default = lake_folder(home).resolve() / DEFAULT_CATALOG["duckdb"]
    return {"catalog": "duckdb", "path": default.as_posix()}


def record(home: Path, chosen: Place) -> None:
    """Record the identity of a lake that has just been opened for the first time. The default
    DuckDB catalog needs no record (its file identifies it, as before #120), so a project
    without a `lake` section gains no file."""
    target = home / RECORD_FILE
    if target.exists() or chosen.identity == _default_identity(home):
        return
    home.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{RECORD_FILE}.{os.getpid()}")
    partial.write_text(json.dumps(chosen.identity, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, target)
