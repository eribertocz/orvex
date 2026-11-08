"""An existing DuckLake's attachment, built from `connections.yaml` and the instance as it stands
(P2-30 PR 3, #399; ADR-0027 §2, §3).

The store attaches and verifies a lake against an `ExistingLakeAttachment`: its catalog and data
location, and the instance's protected set, roles and credentials in use (`lake_separation`).
`current` builds it again on every call, reading the project registry and every registered
project, so each verification of a snapshot sees a project registered after the attach (PR 2,
finding 4). Secrets are referenced by their variables; the one value read here is an Azure
connection string, only to show that it reaches Blob Storage over HTTPS
(`SOURCE_TLS_REQUIRED`), with the strict reading of `lake_place` (each key once, as Azure spells
it). The store checks it again when it opens the lake.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from orvex.contracts.project.v1 import DucklakeConnection, DucklakePostgresCatalog
from orvex.contracts.store.v1 import (
    AzureStorage,
    ExistingLakeAttachment,
    ObjectStorage,
    PostgresLakeLocation,
    SqliteLakeLocation,
)
from orvex.server import lake_place, lake_separation
from orvex.server.project import CONNECTIONS_FILE, ProjectLoadError


def location(
    name: str, connection: DucklakeConnection
) -> SqliteLakeLocation | PostgresLakeLocation:
    """The store's location of the lake: catalog, data location and object storage."""
    storage = _storage(name, connection)
    catalog = connection.catalog.root
    if isinstance(catalog, DucklakePostgresCatalog):
        pairs = {
            "host": catalog.host,
            "port": str(catalog.port),
            "dbname": catalog.database,
            "user": catalog.user,
        }
        if catalog.ssl_mode is not None:
            pairs["sslmode"] = catalog.ssl_mode
        if catalog.ssl_mode == "verify-full":
            root = catalog.ssl_root_cert or "system"
            pairs["sslrootcert"] = (
                "system" if root == "system" else os.environ[_variable(root, name, "ssl_root_cert")]
            )
        return PostgresLakeLocation(
            kind="lake_location",
            catalog="postgres",
            connection=" ".join(f"{key}={lake_place._libpq(v)}" for key, v in pairs.items()),
            password_env=_variable(catalog.password.root, name, "catalog/password"),
            metadata_schema=catalog.schema_,
            data_path=connection.data_path,
            storage=storage,
        )
    return SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=catalog.path.root,
        data_path=connection.data_path,
        storage=storage,
    )


def attachment(
    found: lake_separation.Instance, folder: Path, name: str, connection: DucklakeConnection
) -> ExistingLakeAttachment:
    """The lake `name` of the project at `folder`, against the instance `found`."""
    protection = lake_separation.protection(found, folder, name)
    return ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name=name,
        location=location(name, connection),
        roles_in_use=protection.roles_in_use,
        protected_files=protection.protected_files,
        protected_locations=protection.protected_locations,
        credentials_in_use=protection.credentials_in_use,
    )


def current(
    directory: Path, folder: Path, slug: str, name: str, connection: DucklakeConnection
) -> Callable[[], ExistingLakeAttachment]:
    """The attachment as the instance stands at each call: registry and projects read again."""

    def build() -> ExistingLakeAttachment:
        folders = lake_separation.registered(directory, folder)
        return attachment(
            lake_separation.instance(directory, folders, slug), folder, name, connection
        )

    return build


def _storage(name: str, connection: DucklakeConnection) -> ObjectStorage | None:
    if connection.s3 is not None:
        return ObjectStorage(lake_place._s3(connection.s3))
    if connection.gcs is not None:
        return ObjectStorage(lake_place._gcs(connection.gcs))
    if connection.azure is None:
        return None
    variable = _variable(connection.azure.connection_string.root, name, "azure/connection_string")
    if not lake_place._azure_over_https(os.environ[variable]):
        raise ProjectLoadError(
            "SOURCE_TLS_REQUIRED",
            f"Connection '{name}': the connection string in {variable} does not reach Azure Blob "
            "Storage over HTTPS: use DefaultEndpointsProtocol=https (and an https:// "
            "BlobEndpoint), each key written once and as Azure spells it. Plain HTTP is accepted "
            "only for an endpoint on the local machine.",
            file=CONNECTIONS_FILE,
            path=f"/connections/{name}/azure/connection_string",
            connection=name,
        )
    return ObjectStorage(AzureStorage(type="azure", connection_string_env=variable))


def _variable(reference: str, name: str, field: str) -> str:
    """The variable a `${env:NAME}` reference names, which must be set (its value unread)."""
    match = lake_place._VARIABLE.fullmatch(reference)
    variable = match.group(1) if match is not None else ""
    if not variable or variable not in os.environ:
        raise ProjectLoadError(
            "PROJECT_SECRET_MISSING",
            f"Connection '{name}': {field} names a variable that is not set.",
            file=CONNECTIONS_FILE,
            path=f"/connections/{name}/{field}",
            connection=name,
            variable=variable or None,
        )
    return variable
