"""Where a lake is, checked before anything is opened: PostgreSQL connection strings (no
inline secret, TLS to another machine), object-storage data paths and their credentials, and how
the file sandbox lists a data folder. Shared by the managed lake (`lake`) and an existing lake
(`existing_lake`), moved out of `lake.py` unchanged (#399)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from orvex.contracts.store.v1 import (
    AzureStorage,
    GcsStorage,
    LocalLakeLocation,
    ObjectStorage,
    PostgresLakeLocation,
    S3Storage,
    SqliteLakeLocation,
)
from orvex.store.errors import StoreError, register_secret

# Any password key (password=, sslpassword=, ?password=) and any URI form (user:pw@host) is
# refused: the password may only come from `password_env`.
_INLINE_SECRET = re.compile(r"password|://", re.IGNORECASE)
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")
# Hosts that are the local machine; a host starting with `/` (or `@`, Linux's abstract namespace)
# is a Unix-socket directory, and an empty host is libpq's default socket.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOCAL_ADDRESSES = frozenset({"127.0.0.1", "::1"})

type AnyLakeLocation = LocalLakeLocation | SqliteLakeLocation | PostgresLakeLocation

POSTGRES_CONNECT_TIMEOUT_S = 10


def _conninfo(text: str) -> dict[str, str]:
    """The `keyword=value` pairs of a libpq connection string (values bare or single-quoted,
    with backslash escapes), the last of a repeated keyword winning, as in libpq."""
    pairs: dict[str, str] = {}
    invalid = StoreError(
        "STORE_STATEMENT_INVALID", "The connection must be libpq key=value pairs.", {}
    )
    index, length = 0, len(text)
    while True:
        while index < length and text[index].isspace():
            index += 1
        if index == length:
            return pairs
        start = index
        while index < length and not text[index].isspace() and text[index] != "=":
            index += 1
        keyword = text[start:index]
        while index < length and text[index].isspace():
            index += 1
        if not keyword or index == length or text[index] != "=":
            raise invalid
        index += 1
        while index < length and text[index].isspace():
            index += 1
        value: list[str] = []
        if index < length and text[index] == "'":
            index += 1
            while True:
                if index == length:
                    raise invalid  # unterminated quoted value
                character = text[index]
                if character == "\\" and index + 1 < length:
                    value.append(text[index + 1])
                    index += 2
                elif character == "'":
                    index += 1
                    break
                else:
                    value.append(character)
                    index += 1
        else:
            while index < length and not text[index].isspace():
                if text[index] == "\\" and index + 1 < length:
                    index += 1
                value.append(text[index])
                index += 1
        pairs[keyword.lower()] = "".join(value)


def _inline_secret(connection: str) -> bool:
    """Whether a connection string carries a password: a `password` or `sslpassword` keyword,
    or any URI form (`user:pw@host`, `?password=`). A value that merely contains the word (a
    database named `password_vault`) is not one. A string that is not key=value pairs is
    judged by the word alone."""
    if "://" in connection:
        return True
    try:
        pairs = _conninfo(connection)
    except StoreError:
        return _INLINE_SECRET.search(connection) is not None
    return bool({"password", "sslpassword"} & set(pairs))


def _local_host(host: str) -> bool:
    return host == "" or host.startswith(("/", "@")) or host.lower() in _LOCAL_HOSTS


def _hosts(pairs: dict[str, str]) -> str:
    return pairs["host"] if "host" in pairs else os.environ.get("PGHOST", "")


def _local_connection(pairs: dict[str, str]) -> bool:
    """Whether every host (and `hostaddr`) libpq could use for these connection keywords is the
    local machine. The environment's `PGHOST`/`PGHOSTADDR` count when the keywords name none
    (libpq and DuckDB's postgres extension read them), and a service (`service`, `PGSERVICE`),
    which can name any host, is never local. One rule for catalogs (#120) and sources (#122)."""
    addresses = pairs["hostaddr"] if "hostaddr" in pairs else os.environ.get("PGHOSTADDR", "")
    return (
        "service" not in pairs
        and not os.environ.get("PGSERVICE")
        and all(_local_host(host.strip()) for host in _hosts(pairs).split(","))
        and all(address.strip() in _LOCAL_ADDRESSES for address in addresses.split(",") if address)
    )


def _require_tls(connection: str) -> None:
    """Refuse a PostgreSQL catalog on another machine (`_local_connection`) unless it is reached
    with `sslmode=verify-full`, which encrypts and checks that the certificate is the named
    host's."""
    pairs = _conninfo(connection)
    hosts = _hosts(pairs)
    if _local_connection(pairs) or pairs.get("sslmode") == "verify-full":
        return
    raise StoreError(
        "STORE_TLS_REQUIRED",
        "A PostgreSQL catalog on another machine must be reached with sslmode=verify-full "
        "(TLS that checks the server's certificate and name).",
        {"host": hosts or "(default)", "sslmode": pairs.get("sslmode", "(none)")},
    )


def _with_root_cert(connection: str) -> str:
    """`sslrootcert=system` (the operating system's certificate authorities) unless the
    connection names its own, when TLS is verified."""
    pairs = _conninfo(connection)
    if pairs.get("sslmode") == "verify-full" and "sslrootcert" not in pairs:
        return f"{connection} sslrootcert=system"
    return connection


@dataclass(frozen=True, slots=True)
class _Storage:
    """Object storage settings with their credentials, read from the environment: the key and
    secret (S3, Google Cloud Storage), or the connection string (Azure, in `key_id`)."""

    settings: S3Storage | GcsStorage | AzureStorage
    key_id: str
    secret: str = ""


# The URL scheme of each kind of object storage: the one spelling accepted for a data path.
_SCHEMES = {"s3": "s3://", "gcs": "gs://", "azure": "az://"}


def _data_path(location: AnyLakeLocation) -> str:
    """Where the lake's data files go: `data_path`, or `<path>/data/` for a local lake. An
    object-storage path always ends with `/`, so the secret's `SCOPE` is that folder and not
    every key that starts like it (#147)."""
    if isinstance(location, LocalLakeLocation):
        if location.data_path is None:
            return (Path(location.path) / "data").as_posix() + "/"
        path = location.data_path
    else:
        path = location.data_path
    if "://" in path and not path.endswith("/"):
        return path + "/"
    return path


def _storage_credentials(
    storage: ObjectStorage | S3Storage | GcsStorage | AzureStorage | None, data_path: str
) -> _Storage | None:
    """Check the data location and read its credentials, before anything is opened.

    An object-storage data path (`s3://`, `gs://`, `az://`) needs `storage` of its kind, and
    `storage` needs such a path; any other URL is refused, and so are `.` and `..` segments.
    Unencrypted HTTP is refused unless the endpoint is the local machine
    (`STORE_TLS_REQUIRED`). The credentials are registered so no error can show them.
    """
    settings = storage.root if isinstance(storage, ObjectStorage) else storage
    remote = "://" in data_path
    scheme = next((value for value in _SCHEMES.values() if data_path.startswith(value)), None)
    if remote and scheme is None:
        raise StoreError(
            "STORE_LOCATION_INVALID",
            "Data files can be on a local path or in object storage (s3://, gs://, az://) only.",
            {"data_path": data_path},
        )
    if scheme is not None and any(
        part in {".", ".."} for part in data_path[len(scheme) :].split("/")
    ):
        raise StoreError(
            "STORE_LOCATION_INVALID",
            "An object-storage data path cannot contain . or .. segments.",
            {"data_path": data_path},
        )
    if remote != (settings is not None) or (
        settings is not None and _SCHEMES[settings.type] != scheme
    ):
        raise StoreError(
            "STORE_LOCATION_INVALID",
            "An object-storage data path needs storage settings of its kind (s3:// with s3, "
            "gs:// with gcs, az:// with azure), and storage settings need such a path.",
            {"data_path": data_path},
        )
    if settings is None:
        return None
    if isinstance(settings, AzureStorage):
        connection_string = _variable_value(settings.connection_string_env)
        _check_azure(connection_string)
        return _Storage(settings, connection_string)
    if not settings.use_ssl:
        endpoint = settings.endpoint or ""
        host = endpoint[1 : endpoint.find("]")] if endpoint.startswith("[") else endpoint
        host = host if endpoint.startswith("[") else endpoint.split(":", 1)[0]
        if not host or host.lower() not in _LOCAL_HOSTS:
            raise StoreError(
                "STORE_TLS_REQUIRED",
                "Object storage on another machine must be reached over HTTPS (use_ssl).",
                {"endpoint": settings.endpoint or "(AWS S3)"},
            )
    key_id = _variable_value(settings.key_id_env)
    secret = _variable_value(settings.secret_env)
    return _Storage(settings, key_id, secret)


def _variable_value(variable: str) -> str:
    """A credential from the environment, registered so no error can show it."""
    value = os.environ.get(variable, "")
    if not value:
        raise StoreError(
            "STORE_SECRET_MISSING",
            f"The environment variable {variable} is not set.",
            {"variable": variable},
        )
    register_secret(value)
    return value


# The keys of an Azure connection string as the Azure SDK reads them: exactly, by case, without
# trimming, the last copy winning. A key written another way would be ignored by the SDK while
# a forgiving check read it, so such keys and repeated keys are refused (#171, Reviewer pass 1).
AZURE_KEYS = frozenset(
    {
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
    }
)
_AZURE_KEYS_FOLDED = {key.lower(): key for key in AZURE_KEYS}


def azure_pairs(connection_string: str) -> dict[str, str] | None:
    """The `key=value` pairs of an Azure connection string, read as the Azure SDK reads them;
    None when it is not such pairs (an empty segment included), repeats a key, or writes a known
    key in another case or with spaces around it."""
    pairs: dict[str, str] = {}
    parts = connection_string.split(";")
    if parts[-1] == "":
        parts.pop()  # one final `;`
    for part in parts:
        key, equals, value = part.partition("=")
        # An empty segment (`;;`, a leading `;`) is refused: the SDK reads a key up to the next
        # `=`, across `;`, so the key after it would be hidden from the SDK but not from here.
        if not equals or not key:
            return None
        known = _AZURE_KEYS_FOLDED.get(key.strip().lower())
        if (known is not None and key != known) or key in pairs:
            return None
        pairs[key] = value
    return pairs


def _check_azure(connection_string: str) -> None:
    """An Azure connection string: `key=value` pairs naming an AccountName, over HTTPS unless
    its blob endpoint is the local machine. Its account key is registered too; nothing of it is
    shown in errors."""
    pairs = azure_pairs(connection_string)
    if pairs is None:
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "The Azure connection string must be key=value pairs separated by ;, each key "
            "written once and as Azure spells it (AccountName, AccountKey, BlobEndpoint, …).",
            {},
        )
    for name in ("AccountKey", "SharedAccessSignature"):
        if pairs.get(name):
            register_secret(pairs[name])
    if not pairs.get("AccountName"):
        raise StoreError(
            "STORE_STATEMENT_INVALID", "The Azure connection string must name an AccountName.", {}
        )
    blob = pairs.get("BlobEndpoint")
    if blob is not None:
        scheme, separator, rest = blob.partition("://")
        host_port = rest.split("/", 1)[0]
        host = (
            host_port[1 : host_port.find("]")]
            if host_port.startswith("[")
            else host_port.split(":", 1)[0]
        )
        local = scheme in {"http", "https"} and host.lower() in _LOCAL_HOSTS
        if not separator or (scheme != "https" and not local):
            raise StoreError(
                "STORE_TLS_REQUIRED",
                "Azure Blob Storage on another machine must be reached over HTTPS "
                "(the connection string's BlobEndpoint).",
                {},
            )
        if local:
            return  # an emulator on this machine: its endpoint decides the protocol
    if pairs.get("DefaultEndpointsProtocol", "https") != "https":
        raise StoreError(
            "STORE_TLS_REQUIRED",
            "Azure Blob Storage on another machine must be reached over HTTPS "
            "(the connection string's DefaultEndpointsProtocol).",
            {},
        )


def _postgres_password(location: PostgresLakeLocation) -> str:
    """Refuse an unsafe PostgreSQL location, then read and register its password."""
    if _inline_secret(location.connection):
        raise StoreError(
            "STORE_SECRET_INLINE",
            "The connection must be key=value pairs without any password; use password_env.",
            {},
        )
    _require_tls(location.connection)
    if location.metadata_schema is not None and not _SCHEMA_NAME.fullmatch(
        location.metadata_schema
    ):
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "metadata_schema must match [a-z_][a-z0-9_]*.",
            {"metadata_schema": location.metadata_schema},
        )
    password = os.environ.get(location.password_env)
    if not password:
        raise StoreError(
            "STORE_SECRET_MISSING",
            f"The environment variable {location.password_env} is not set.",
            {"variable": location.password_env},
        )
    register_secret(password)
    return password


def _sandbox_folder(data_path: str) -> str:
    """A data path as the sandbox lists it: an object-storage prefix as written (DuckDB checks
    remote paths against it too), a local folder resolved."""
    return data_path if "://" in data_path else Path(data_path).resolve().as_posix()
