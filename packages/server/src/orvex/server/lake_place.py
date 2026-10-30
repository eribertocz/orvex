"""Where a project's lake lives (#120, ADR-0027 §2).

`project.yaml`'s optional `lake.catalog` chooses the catalog: a DuckDB file (the default), a
SQLite file, or PostgreSQL. This module turns it into the store's location, refuses what
ADR-0027 does not allow before anything is opened or created, and fixes the choice when the
lake is created: `.<slug>/lake.location` records the catalog's identity, and a project whose
configuration no longer matches it is refused instead of silently getting a second, empty lake.

`lake.data` places the data files (#126): the default `.<slug>/lake/data/`, another local folder
of their own, or object storage (S3-compatible, Google Cloud Storage #127, Azure Blob Storage
#127); `lake.encrypted` encrypts them.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from orvex.contracts.project.v1 import (
    AzureStorage,
    DuckdbLakeCatalog,
    GcsStorage,
    Lake,
    LakeData,
    PostgresLakeCatalog,
    S3Storage,
    SecretReference,
    SqliteLakeCatalog,
)
from orvex.contracts.store.v1 import AzureStorage as StoreAzureStorage
from orvex.contracts.store.v1 import GcsStorage as StoreGcsStorage
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    ObjectStorage,
    PostgresLakeLocation,
    SqliteLakeLocation,
)
from orvex.contracts.store.v1 import S3Storage as StoreS3Storage
from orvex.server.project import (
    FILE_TYPES,
    LOCAL_HOSTS,
    PROJECT_FILE,
    Project,
    ProjectLoadError,
    is_local_host,
)

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
_GLOB_MARK = re.compile(r"[*?\[{]")
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
    if lake is None:
        # No `lake` section: exactly as before #120.
        return Place(
            LocalLakeLocation(kind="lake_location", catalog="local", path=str(lake_folder(home))),
            _default_identity(home),
        )
    chosen = _catalog_place(lake, project, home)
    return _with_data(chosen, lake, project, home.resolve())


def _catalog_place(lake: Lake, project: Project, home: Path) -> Place:
    """The catalog's place, its data files in the default folder."""
    catalog = lake.catalog.root if lake.catalog is not None else DuckdbLakeCatalog(type="duckdb")
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


def _with_data(chosen: Place, lake: Lake, project: Project, home: Path) -> Place:
    """The place with `lake.data` and `lake.encrypted` applied (#126). Defaults change neither
    the location nor the identity, so a lake recorded before #126 keeps its identity."""
    data_path, storage = _data(lake.data, project, home)
    updates: dict[str, object] = {}
    identity = dict(chosen.identity)
    if data_path is not None:
        updates["data_path"] = data_path
        identity["data"] = data_path
    if storage is not None:
        updates["storage"] = storage
        # The service is part of the lake's identity (#147, decision #182): the same bucket
        # and prefix on another service is another lake, which maintenance must not clean.
        identity["endpoint"] = _endpoint(storage)
    if lake.encrypted:
        updates["encrypted"] = True
        identity["encrypted"] = "yes"
    if not updates:
        return chosen
    return Place(chosen.location.model_copy(update=updates), identity)


# The URL scheme of each kind of object storage, and its `lake.data` block: the one spelling
# accepted for each (#127: `gcs://`, `azure://`, `abfss://`, `r2://` are refused).
_OBJECT_STORAGE = {"s3://": "s3", "gs://": "gcs", "az://": "azure"}
GCS_ENDPOINT = "storage.googleapis.com"


def _data(
    data: LakeData | None, project: Project, home: Path
) -> tuple[str | None, ObjectStorage | None]:
    """The data files' location (None: the default `.<slug>/lake/data/`) and its object
    storage, refused unless it is a local folder or object storage over HTTPS."""
    pointer = "/lake/data/path"
    if data is None:
        return None, None
    text = data.path
    blocks = {"s3": data.s3, "gcs": data.gcs, "azure": data.azure}
    given = [name for name, block in blocks.items() if block is not None]
    scheme = next((prefix for prefix in _OBJECT_STORAGE if text.startswith(prefix)), None)
    if scheme is not None:
        kind = _OBJECT_STORAGE[scheme]
        if given != [kind]:
            raise _invalid(
                f"lake.data.path {text}: {scheme} needs the lake.data.{kind} settings, and no "
                "other storage settings.",
                pointer,
            )
        bucket = text[len(scheme) :].split("/", 1)[0]
        if not bucket:
            noun = "container" if kind == "azure" else "bucket"
            raise _invalid(
                f"lake.data.path {text}: name a {noun} ({scheme}{noun}/prefix/).", pointer
            )
        if any(part in {".", ".."} for part in text[len(scheme) :].split("/")):
            # `s3://bucket/prefix/../` would make the whole bucket the lake's prefix, and
            # orphan cleanup would then delete files across it.
            raise _invalid(
                f"lake.data.path {text}: an object-storage path cannot contain . or .. segments.",
                pointer,
            )
        prefix = text if text.endswith("/") else text + "/"
        if data.s3 is not None:
            return prefix, ObjectStorage(_s3(data.s3))
        if data.gcs is not None:
            return prefix, ObjectStorage(_gcs(data.gcs))
        assert data.azure is not None
        return prefix, ObjectStorage(_azure(data.azure))
    if _URL.match(text):
        raise _invalid(
            f"lake.data.path {text}: data files can be in a local folder (or a network file "
            "system mounted there), in S3-compatible storage (s3://), Google Cloud Storage "
            "(gs://) or Azure Blob Storage (az://).",
            pointer,
        )
    if given:
        raise _invalid(
            f"lake.data.path {text}: lake.data.{given[0]} applies to object storage only.",
            f"/lake/data/{given[0]}",
        )
    expanded = Path(os.path.expanduser(text))
    path = (expanded if expanded.is_absolute() else project.folder / expanded).resolve()
    if path == lake_folder(home) / "data":
        return None, None  # the default, named explicitly
    overlap = _overlap(path, project, home)
    if overlap is not None:
        raise _invalid(
            f"lake.data.path {text}: the lake's data folder must hold only the lake's files, "
            f"but it {overlap}. Maintenance deletes Parquet files there that the lake does not "
            "know. Choose a folder of its own.",
            pointer,
        )
    return path.as_posix() + "/", None


def _overlap(path: Path, project: Project, home: Path) -> str | None:
    """How a local data folder overlaps other files, or None. DuckLake's orphan cleanup deletes
    every untracked `.parquet` file under the data folder, so it must not hold anything else."""
    folder = project.folder.resolve()
    if folder.is_relative_to(path):
        return f"contains the project folder {folder}"
    if path.is_relative_to(home):
        return f"is inside {home}, which holds the lake's own files"
    for name, connection in project.connections.items():
        if not isinstance(connection, FILE_TYPES):
            continue  # a database server, not a file
        source = connection.path.root  # absolute: the loader resolves it
        named = Path(source).as_posix()
        glob = _GLOB_MARK.search(named)
        if glob is None:
            if Path(named).resolve().is_relative_to(path):
                return f"contains the file of connection {name}"
            continue
        base = Path(named[: glob.start()].rsplit("/", 1)[0] or "/").resolve()
        if base.is_relative_to(path) or path.is_relative_to(base):
            return f"overlaps the files connection {name} reads ({source})"
    return None


def _s3(settings: S3Storage) -> StoreS3Storage:
    pointer = "/lake/data/s3"
    use_ssl = settings.use_ssl is not False
    if not use_ssl:
        endpoint = settings.endpoint or ""
        if not endpoint or endpoint_host(endpoint).lower() not in LOCAL_HOSTS:
            raise ProjectLoadError(
                "LAKE_TLS_REQUIRED",
                f"lake.data.s3: use_ssl: false is accepted only for an endpoint on the local "
                f"machine (found: {endpoint or 'AWS S3'}); object storage is reached over HTTPS.",
                file=PROJECT_FILE,
                path=f"{pointer}/use_ssl",
            )
    key_id = _variable(settings.key_id, f"{pointer}/key_id")
    secret = _variable(settings.secret, f"{pointer}/secret")
    return StoreS3Storage(
        type="s3",
        region=settings.region,
        endpoint=settings.endpoint,
        url_style=settings.url_style,
        use_ssl=use_ssl,
        key_id_env=key_id,
        secret_env=secret,
    )


def _gcs(settings: GcsStorage) -> StoreGcsStorage:
    """Google Cloud Storage through its S3-compatible API, always at its own endpoint over
    HTTPS: a project cannot point it elsewhere."""
    pointer = "/lake/data/gcs"
    return StoreGcsStorage(
        type="gcs",
        endpoint=GCS_ENDPOINT,
        use_ssl=True,
        key_id_env=_variable(settings.key_id, f"{pointer}/key_id"),
        secret_env=_variable(settings.secret, f"{pointer}/secret"),
    )


def _azure(settings: AzureStorage) -> StoreAzureStorage:
    """Azure Blob Storage with a connection string, which must use HTTPS unless its blob
    endpoint is the local machine. The connection string is read here only to check that; it
    is never shown."""
    pointer = "/lake/data/azure/connection_string"
    variable = _variable(settings.connection_string, pointer)
    if not _azure_over_https(os.environ[variable]):
        raise ProjectLoadError(
            "LAKE_TLS_REQUIRED",
            f"lake.data.azure: the connection string in {variable} does not reach Azure Blob "
            "Storage over HTTPS: use DefaultEndpointsProtocol=https (and an https:// "
            "BlobEndpoint), each key written once and as Azure spells it. Plain HTTP is "
            "accepted only for an endpoint on the local machine.",
            file=PROJECT_FILE,
            path=pointer,
        )
    blob = _connection_pairs(os.environ[variable]).get("BlobEndpoint")
    if blob is not None:
        _without_userinfo(blob, f"lake.data.azure: the BlobEndpoint in {variable}", pointer)
    return StoreAzureStorage(type="azure", connection_string_env=variable)


# The keys of an Azure connection string as the Azure SDK reads them (by case, untrimmed, the
# last copy winning): read the same way here, so the check sees what the SDK will use.
_AZURE_KEYS = (
    "DefaultEndpointsProtocol",
    "AccountName",
    "AccountKey",
    "SharedAccessSignature",
    "EndpointSuffix",
    "BlobEndpoint",
    "BlobSecondaryEndpoint",
    "QueueEndpoint",
    "QueueSecondaryEndpoint",
    "TableEndpoint",
    "TableSecondaryEndpoint",
    "FileEndpoint",
    "FileSecondaryEndpoint",
    "DfsEndpoint",
    "DfsSecondaryEndpoint",
)


def _azure_over_https(connection_string: str) -> bool:
    """Whether a connection string reaches Blob Storage over HTTPS, or an endpoint on the local
    machine. A key repeated, or a known key written in another case or with spaces around it,
    counts as no: the SDK would read another value than this check (#171, Reviewer pass 1). A
    string that is not `key=value` pairs (an empty segment included) counts as no too."""
    folded = {key.lower(): key for key in _AZURE_KEYS}
    pairs: dict[str, str] = {}
    parts = connection_string.split(";")
    if parts[-1] == "":
        parts.pop()  # one final `;`
    for part in parts:
        key, equals, value = part.partition("=")
        # An empty segment or one without `=` counts as no: the SDK reads a key up to the next
        # `=`, across `;`, and would not see the key after it (#171, Reviewer pass 2).
        if not equals or not key:
            return False
        known = folded.get(key.strip().lower())
        if (known is not None and key != known) or key in pairs:
            return False
        pairs[key] = value
    blob = pairs.get("BlobEndpoint")
    if blob is not None:
        scheme, _, rest = blob.partition("://")
        host_port = rest.split("/", 1)[0]
        host = (
            host_port[1 : host_port.find("]")]
            if host_port.startswith("[")
            else host_port.split(":", 1)[0]
        )
        if scheme in {"http", "https"} and host.lower() in LOCAL_HOSTS:
            return True
        if scheme != "https":
            return False
    return pairs.get("DefaultEndpointsProtocol", "https") == "https"


def _endpoint(storage: ObjectStorage) -> str:
    """The object-storage service a lake's data files are on, as the identity compares it:
    lowercased, without scheme or trailing `/`. AWS S3 (no endpoint) is the empty string,
    Google Cloud Storage its own endpoint, Azure the blob endpoint its connection string names
    (`BlobEndpoint`, else `<AccountName>.blob.<EndpointSuffix>`)."""
    settings = storage.root
    if isinstance(settings, StoreAzureStorage):
        pairs = _connection_pairs(os.environ.get(settings.connection_string_env, ""))
        blob = pairs.get("BlobEndpoint")
        if blob is None:
            suffix = pairs.get("EndpointSuffix", "core.windows.net")
            blob = f"{pairs.get('AccountName', '')}.blob.{suffix}"
        return _normalized_endpoint(blob)
    return _normalized_endpoint(settings.endpoint or "")


def _connection_pairs(connection_string: str) -> dict[str, str]:
    """An Azure connection string's `key=value` pairs (its shape is checked by
    `_azure_over_https`)."""
    return dict(part.partition("=")[::2] for part in connection_string.split(";") if part)


def _normalized_endpoint(endpoint: str) -> str:
    """`host[:port][/path]`, lowercased, without scheme, credentials, query, fragment or trailing
    `/`: the record and its messages never hold a user, password or SAS token an Azure
    `BlobEndpoint` may carry (#147, Reviewer pass 1)."""
    text = endpoint.strip()
    address = text.split("://", 1)[1] if "://" in text else text
    # `user:password@host`: everything up to the last `@` goes first, even a `/`, `?` or `#`
    # in a password (Reviewer pass 2). An endpoint with `@` is refused before it is recorded
    # (`_without_userinfo`); this keeps the record clean whatever reaches it.
    address = address.rpartition("@")[2]
    for mark in ("?", "#"):
        address = address.split(mark, 1)[0]
    return address.lower().rstrip("/")


def _without_userinfo(endpoint: str, where: str, pointer: str) -> None:
    """Refuse an endpoint that carries a user or password (`user:password@host`): no object
    store needs one there, and the lake's record and messages show the endpoint (#147,
    Reviewer pass 2). Credentials belong in their own `${env:NAME}` fields."""
    if "@" in endpoint:
        raise ProjectLoadError(
            "LAKE_LOCATION_INVALID",
            f"{where}: the endpoint carries a user or password (an @); give only the host, "
            "port and path, and the credentials in their own variables.",
            file=PROJECT_FILE,
            path=pointer,
        )


def endpoint_host(endpoint: str) -> str:
    """The host of `host[:port]` or `[ipv6][:port]`."""
    if endpoint.startswith("["):
        return endpoint[1 : endpoint.find("]")]
    return endpoint.split(":", 1)[0]


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
    local = is_local_host(host)
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
            f"Environment variable {variable} is not set ({pointer.strip('/').replace('/', '.')}).",
            file=PROJECT_FILE,
            path=pointer,
            variable=variable,
        )
    return variable


def _libpq(value: str) -> str:
    """A libpq connection-string value, always quoted: no value can add a keyword."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# The state of a record written before the lake's first open, until that open succeeds (#146).
CREATING = "creating"


def _read_record(home: Path) -> tuple[dict[str, str | int], bool] | None:
    """The record's identity and whether its first open is unfinished (`creating`); None when
    there is no record file. A record that cannot be read is refused: it says where the lake
    is, so nothing is opened until it is restored."""
    record = home / RECORD_FILE
    try:
        found = json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ProjectLoadError(
            "LAKE_LOCATION_CHANGED",
            f"{RECORD_FILE} cannot be read ({type(error).__name__}); it records where this "
            "project's lake is, so nothing is opened until it is restored.",
            file=f"{home.name}/{RECORD_FILE}",
        ) from None
    if not isinstance(found, dict) or found.get("state", CREATING) != CREATING:
        raise ProjectLoadError(
            "LAKE_LOCATION_CHANGED",
            f"{RECORD_FILE} is not a lake record; nothing is opened until it is restored.",
            file=f"{home.name}/{RECORD_FILE}",
        )
    creating = "state" in found
    found.pop("state", None)
    return found, creating


def recorded(home: Path) -> dict[str, str | int] | None:
    """The identity of the project's existing lake, or None when it has none yet. A lake made
    before #120 has no record: its catalog is the default DuckDB file."""
    read = _read_record(home)
    if read is not None:
        return read[0]
    if (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists():
        return _default_identity(home)
    return None


def exists(home: Path) -> bool:
    """Whether the project has a lake: a confirmed record, or the default catalog file. A record
    whose first open did not finish is not a lake yet (#146)."""
    if (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists():
        return True
    try:
        read = _read_record(home)
    except ProjectLoadError:
        return True  # a record that cannot be read: the commands that open the lake report it
    if read is None or _unfinished_default(home, read):
        return _orphan_data(home)  # a default lake made before #204 that lost its catalog
    return not read[1]


def _unfinished_default(home: Path, read: tuple[dict[str, str | int], bool]) -> bool:
    """Whether a record is the default lake's, marked `creating`: the first open after #204 of
    a default lake made before it, which did not finish (#204, Reviewer pass 1). Its data files
    are the lake's as much as with no record."""
    return read[1] and _same(read[0], _default_identity(home))


def orphan_data(home: Path) -> str | None:
    """The default data folder, when it holds the data files of a default lake whose catalog
    file is gone and that has no confirmed record (#204)."""
    try:
        read = _read_record(home)
    except ProjectLoadError:
        return None
    if read is not None and not _unfinished_default(home, read):
        return None
    return (lake_folder(home) / "data").as_posix() if _orphan_data(home) else None


def _orphan_data(home: Path) -> bool:
    """Whether the default data folder holds data files while there is neither a record nor
    the default catalog: a default lake made before #204 whose catalog file is gone (#204)."""
    if (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists():
        return False
    data = lake_folder(home) / "data"
    return data.is_dir() and next(data.rglob("*.parquet"), None) is not None


def _nothing_created(identity: dict[str, str | int]) -> bool:
    """Whether a lake with this identity provably does not exist: a DuckDB or SQLite catalog
    whose file is absent. A PostgreSQL catalog cannot be told from here."""
    if identity.get("catalog") not in {"duckdb", "sqlite"}:
        return False
    return not Path(str(identity.get("path", ""))).exists()


def check_unchanged(home: Path, chosen: Place) -> None:
    """Refuse a configuration whose catalog, data folder or encryption is not the one the
    existing lake was created with, naming each setting that changed. A record whose first open
    did not finish binds only if a lake may exist there (#146)."""
    read = _read_record(home)
    if read is not None and read[1] and _nothing_created(read[0]):
        read = None  # an unfinished first open that created nothing: as if no record (#181)
    if read is None:
        existing = None
        if (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists():
            existing = _default_identity(home)
        creating = False
    else:
        existing, creating = read
    current = chosen.identity
    if existing is None or _same(existing, current):
        return
    current = _as_recorded(existing, current)
    changes = []
    if _catalog_identity(existing) != _catalog_identity(current):
        changes.append(f"lake.catalog was {_describe(existing)}, now {_describe(current)}")
    if existing.get("data") != current.get("data"):
        changes.append(f"lake.data.path was {_data_text(existing)}, now {_data_text(current)}")
    # Only between two object stores: a move to or from a local folder is the data path's line.
    if (
        "endpoint" in existing
        and "endpoint" in current
        and existing.get("endpoint") != current.get("endpoint")
    ):
        changes.append(
            f"lake.data's object-storage endpoint was {existing.get('endpoint') or 'AWS S3'}, "
            f"now {current.get('endpoint') or 'AWS S3'}"
        )
    if existing.get("encrypted") != current.get("encrypted"):
        changes.append(
            f"lake.encrypted was {'true' if existing.get('encrypted') else 'false'}, "
            f"now {'true' if current.get('encrypted') else 'false'}"
        )
    if creating:
        raise ProjectLoadError(
            "LAKE_LOCATION_CHANGED",
            f"a first open of this project's lake with other settings did not finish: "
            f"{'; '.join(changes)}. A lake may already exist there, so nothing was opened or "
            f"created. If no lake was created there, delete {home.name}/{RECORD_FILE} to use "
            "the new settings.",
            file=PROJECT_FILE,
            path="/lake",
        )
    raise ProjectLoadError(
        "LAKE_LOCATION_CHANGED",
        f"this project's lake was created with other settings: {'; '.join(changes)}. A lake is "
        "not moved or re-encrypted; restore the previous settings to use it (nothing was "
        "opened or created).",
        file=PROJECT_FILE,
        path="/lake",
    )


def _same(existing: dict[str, str | int], current: dict[str, str | int]) -> bool:
    """Equal identities, a PostgreSQL host compared without letter case (#146). A record
    written before #147 has no object-storage endpoint: it matches any, and `complete` adds the
    configured one."""
    return _comparable(existing) == _comparable(_as_recorded(existing, current))


def _as_recorded(
    existing: dict[str, str | int], current: dict[str, str | int]
) -> dict[str, str | int]:
    """`current` without the keys a record written before them does not have (#147)."""
    if "endpoint" in existing:
        return current
    return {key: value for key, value in current.items() if key != "endpoint"}


def complete(home: Path, chosen: Place) -> None:
    """Add the object-storage endpoint to a confirmed record written before #147, once the lake
    it names has opened with the configured endpoint. Called under the project lock."""
    read = _read_record(home)
    if read is None or read[1] or "endpoint" in read[0] or "endpoint" not in chosen.identity:
        return
    if _same(read[0], chosen.identity):
        _write(home, dict(chosen.identity))


def _comparable(identity: dict[str, str | int]) -> dict[str, str | int]:
    found = dict(identity)
    host = found.get("host")
    # Network host names ignore letter case; a Unix socket folder (`/…`) does not.
    if found.get("catalog") == "postgres" and isinstance(host, str) and not host.startswith("/"):
        found["host"] = host.casefold()
    return found


def _catalog_identity(identity: dict[str, str | int]) -> dict[str, str | int]:
    return {
        key: value
        for key, value in _comparable(identity).items()
        if key not in {"data", "encrypted", "endpoint"}
    }


def _data_text(identity: dict[str, str | int]) -> str:
    return str(identity.get("data", "the default folder"))


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


def _write(home: Path, content: dict[str, str | int]) -> None:
    """Write the record atomically: a reader sees the old file or the new one, never part. A
    write that fails (a full disk, a read-only folder) is `LAKE_LOCATION_CHANGED` (#192)."""
    target = home / RECORD_FILE
    partial = target.with_name(f".{RECORD_FILE}.{os.getpid()}")
    try:
        home.mkdir(parents=True, exist_ok=True)
        with partial.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(content, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())  # on disk before it replaces the record
        os.replace(partial, target)
    except OSError as error:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)  # no half-written file left behind
        raise _unwritable(home, error) from None


def _remove(home: Path) -> None:
    """Remove the record; a removal that fails is `LAKE_LOCATION_CHANGED` (#192)."""
    try:
        (home / RECORD_FILE).unlink(missing_ok=True)
    except OSError as error:
        raise _unwritable(home, error) from None


def _unwritable(home: Path, error: OSError) -> ProjectLoadError:
    return ProjectLoadError(
        "LAKE_LOCATION_CHANGED",
        f"{RECORD_FILE} cannot be written ({type(error).__name__}); it records where this "
        "project's lake is, so the lake was not opened.",
        file=f"{home.name}/{RECORD_FILE}",
    )


def missing_catalog(home: Path) -> str | None:
    """The catalog file of the project's confirmed lake when it is missing (#192): a DuckDB or
    SQLite catalog holds the lake's history, and opening its place again would create a new,
    empty one. With no record, the default catalog when its data folder holds data files but the
    catalog is gone (a default lake made before #204). None when there is no such lake, its
    catalog is PostgreSQL, or its file is there. A `creating` record is not a lake yet
    (#146), except the default lake's, which is read as no record (Reviewer pass 1)."""
    read = _read_record(home)
    if read is None or _unfinished_default(home, read):
        if _orphan_data(home):
            return (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).as_posix()
        return None
    if read[1]:
        return None
    identity = read[0]
    if identity.get("catalog") not in {"duckdb", "sqlite"}:
        return None
    path = Path(str(identity.get("path", "")))
    return None if path.exists() else path.as_posix()


def begin(home: Path, chosen: Place) -> None:
    """Before the lake is opened for writing: record its identity, as `creating`, when it has
    no record yet (#146), so a stop between creating the lake and recording it cannot leave a
    lake without one. The default DuckDB lake is recorded too (#204), so a lost catalog file is
    refused, not recreated. A `creating` record of another identity is replaced only when
    `check_unchanged` let it through (nothing was created there)."""
    read = _read_record(home)
    stale = read is not None and read[1] and _nothing_created(read[0])
    if read is not None and not (stale and not _same(read[0], chosen.identity)):
        return
    _write(home, {**chosen.identity, "state": CREATING})


def adopt(home: Path, chosen: Place) -> None:
    """Record a default lake made before #204, which has its catalog file and no record, as
    confirmed: from then on a lost catalog file is refused (#204). Called under the project
    lock, by the commands that open an existing lake. A `creating` record of the default lake
    (its first open after #204 failed) is confirmed the same way, since its catalog is there
    (Reviewer pass 1); any other record is left as it is."""
    read = _read_record(home)
    if read is not None and not _unfinished_default(home, read):
        return
    if not (lake_folder(home) / DEFAULT_CATALOG["duckdb"]).exists():
        return
    if _same(_default_identity(home), chosen.identity):
        _write(home, dict(chosen.identity))


def confirm(home: Path, chosen: Place) -> None:
    """After the lake opened: a `creating` record of this identity becomes the lake's record."""
    read = _read_record(home)
    if read is not None and read[1] and _same(read[0], chosen.identity):
        _write(home, dict(read[0]))


def abandon(home: Path, chosen: Place) -> None:
    """After a first open failed: remove its `creating` record when nothing was created there
    (a DuckDB or SQLite catalog file that does not exist); otherwise it stays (#146)."""
    read = _read_record(home)
    if (
        read is not None
        and read[1]
        and _same(read[0], chosen.identity)
        and _nothing_created(read[0])
    ):
        _remove(home)
