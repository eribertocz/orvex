"""The managed lake connection's isolation, set once when it opens: the extensions it loads, the
lake and file sources it attaches, its storage secret, and its file sandbox.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

import duckdb
from orvex.contracts.store.v1 import (
    AzureStorage,
    FileSourceAttachment,
    GcsStorage,
    LocalLakeLocation,
    PostgresLakeLocation,
    S3Storage,
    SqliteLakeLocation,
)
from orvex.store.errors import StoreError, _source_not_found
from orvex.store.file_sources import view_query
from orvex.store.lake_names import LAKE
from orvex.store.locations import AnyLakeLocation, _sandbox_folder, _Storage, _with_root_cert
from orvex.store.sql_text import identifier as _identifier
from orvex.store.sql_text import literal as _quote

# The lake's one-row query on a short-lived cursor of its connection (`Lake._one`).
type One = Callable[[str, dict[str, object]], tuple[object, ...] | None]

EXTENSIONS = ("icu", "ducklake")
_GLOB_IN_NAME = re.compile(r"[*?\[]")


def _sandbox_entries(
    location: AnyLakeLocation, spill: str, readable: Sequence[str] = ()
) -> tuple[list[str], list[str]]:
    """What the lake's connection may open, as (folders, files): the spill folder, the lake's
    folder or local data folder, a catalog file with its companion files, and the declared source
    files. Nothing else; DuckDB then adds entries of its own for each database it attaches (its
    `.wal.checkpoint` and `.wal.recovery` files) and for its temporary folder."""
    folders = [spill]
    files = list(readable)
    if isinstance(location, LocalLakeLocation):
        folders.append(Path(location.path).resolve().as_posix())
        if location.catalog_path is not None:
            catalog = Path(location.catalog_path).resolve().as_posix()
            files += [catalog, f"{catalog}.wal"]
        if location.data_path is not None:  # data files outside the lake's folder (#126)
            folders.append(_sandbox_folder(location.data_path))
    else:
        folders.append(_sandbox_folder(location.data_path))
    if isinstance(location, SqliteLakeLocation):
        catalog = Path(location.path).resolve().as_posix()
        files += [catalog, *(f"{catalog}-{suffix}" for suffix in ("wal", "shm", "journal"))]
    return folders, files


class Isolation:
    """The setup of one lake connection, in the order `Lake` calls it; `spill` is the lake's
    private folder, where source catalogs are written."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, spill: str, one: One) -> None:
        self._connection = connection
        self._spill = spill
        self._one = one

    def attach_file_source(self, source: FileSourceAttachment) -> list[str]:
        """Attach one file source read-only; returns the files the sandbox must allow.

        For a glob, exactly the files it matches now: a file added later to the same folder,
        or any other file there, stays unreadable.
        """
        path = source.path
        matched = self._one("SELECT list(file ORDER BY file) FROM glob($path)", {"path": path})
        matched_files = matched[0] if matched else None
        if not isinstance(matched_files, list) or not matched_files:
            raise _source_not_found(source)
        # DuckDB's readers expand `*`, `?` and `[` in each listed name again: `r[1].parquet`
        # would be read as `r1.parquet` (measured on 2.0.0). Such a name is refused, as #82
        # specifies; a bracket class (`r[[]1].parquet`) would read it literally (#232).
        expanded = next(
            (str(file) for file in matched_files if _GLOB_IN_NAME.search(str(file))), None
        )
        if expanded is not None:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source {source.name} matches the file {expanded}, whose name has *, ? or [: "
                "DuckDB would read it as a pattern, so another file could be read in its place. "
                "Rename the file.",
                {"source": source.name, "file": expanded},
            )
        # The view lives in its own small database file, attached read-only: nothing can be
        # created in, or dropped from, a source catalog.
        folder = Path(self._spill) / "sources"
        folder.mkdir(exist_ok=True)
        catalog = (folder / f"{source.name}.duckdb").as_posix()
        builder = duckdb.connect(catalog)
        try:
            # The files matched now, listed one by one: the sandbox allows exactly these.
            # Every column carries the files' values exactly or fails when read (law 11).
            query = view_query(builder, source.type, source.name, [str(f) for f in matched_files])
            builder.sql(f"CREATE VIEW data AS {query}")
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source {source.name} cannot be read as {source.type}.",
                {"source": source.name, "duckdb": str(error)},
            ) from None
        finally:
            builder.close()
        self._source_sql(
            source, f"ATTACH {_quote(catalog)} AS {_identifier(source.name)} (READ_ONLY)"
        )
        return [str(file) for file in matched_files]

    def _source_sql(self, source: FileSourceAttachment, sql: str) -> None:
        try:
            self._connection.sql(sql)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source {source.name} could not be attached.",
                {"source": source.name, "duckdb": str(error)},
            ) from None

    def sandbox(
        self,
        location: AnyLakeLocation,
        readable: Sequence[str] = (),
    ) -> None:
        """Limit file access to the lake's own folders, its catalog file and the declared source
        files, for good (DuckDB cannot undo it)."""
        folders, files = _sandbox_entries(location, self._spill, readable)
        listed = ", ".join(_quote(folder.rstrip("/") + "/") for folder in folders)
        self._connection.sql(f"SET temp_directory = {_quote(self._spill)}")
        self._connection.sql(f"SET allowed_directories = [{listed}]")
        if files:
            self._connection.sql(f"SET allowed_paths = [{', '.join(map(_quote, files))}]")
        self._connection.sql("SET enable_external_access = false")

    def load_extensions(self) -> None:
        for extension in EXTENSIONS:
            self._load_extension(extension)

    def attach_local(self, folder: Path, catalog_path: str | None, options: Sequence[str]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        if catalog_path is None:
            catalog = (folder / "catalog.ducklake").as_posix()
        else:
            Path(catalog_path).parent.mkdir(parents=True, exist_ok=True)
            catalog = Path(catalog_path).as_posix()
        # Paths come from the project configuration, not from query input; quoted as literals.
        self._attach(f"ATTACH {_quote('ducklake:' + catalog)} AS {LAKE} ({', '.join(options)})")

    def _attach(self, sql: str) -> None:
        """Attach the lake; object storage that refuses the credentials or cannot be reached is
        reported without them."""
        try:
            self._connection.sql(sql)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_CATALOG_UNAVAILABLE",
                "The lake could not be opened (its catalog or its data files).",
                {"duckdb": str(error)},
            ) from None

    def storage_secret(self, storage: _Storage, data_path: str) -> None:
        """A temporary secret for the lake's prefix only; never persisted."""
        settings = storage.settings
        if isinstance(settings, AzureStorage):
            self._load_extension("azure")
            options = [
                "TYPE azure",
                f"CONNECTION_STRING {_quote(storage.key_id)}",
                f"SCOPE {_quote(data_path)}",
            ]
        else:
            self.load_httpfs()
            options = [
                f"TYPE {settings.type}",
                f"KEY_ID {_quote(storage.key_id)}",
                f"SECRET {_quote(storage.secret)}",
                f"USE_SSL {'true' if settings.use_ssl else 'false'}",
                f"SCOPE {_quote(data_path)}",
            ]
            if isinstance(settings, S3Storage):
                options.append(f"REGION {_quote(settings.region)}")
                if settings.url_style is not None:
                    options.append(f"URL_STYLE {_quote(settings.url_style)}")
            if settings.endpoint is not None:
                options.append(f"ENDPOINT {_quote(settings.endpoint)}")
            if isinstance(settings, GcsStorage):
                # The endpoint is reached by path (`host/bucket/key`), as the S3 API of Cloud
                # Storage and local test servers both accept.
                options.append("URL_STYLE 'path'")
        try:
            self._connection.sql(f"CREATE TEMPORARY SECRET lake_storage ({', '.join(options)})")
        except duckdb.Error as error:
            raise StoreError(
                "STORE_CATALOG_UNAVAILABLE",
                "The lake's object storage settings were refused.",
                {"duckdb": str(error)},
            ) from None

    def load_httpfs(self) -> None:
        """httpfs, for S3 and Google Cloud Storage and for writing encrypted files, with its web
        file systems turned off: it also opens http://, https:// and hf:// addresses as files,
        and the statement check lets web addresses through as ordinary text (#136, Reviewer
        passes 4 and 5). Set after the extension loads: it names its file systems."""
        self._load_extension("httpfs")
        self._connection.sql("SET disabled_filesystems = 'HTTPFileSystem,HuggingFaceFileSystem'")

    def attach_sqlite_catalog(self, location: SqliteLakeLocation, options: Sequence[str]) -> None:
        """Attach a lake whose catalog is a SQLite file (several processes on one machine)."""
        try:
            Path(location.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            # A file where the folder goes, no permission, …: the catalog cannot be reached.
            raise StoreError(
                "STORE_CATALOG_UNAVAILABLE",
                "The lake's SQLite catalog folder cannot be created.",
                {"error": type(error).__name__},
            ) from None
        self._load_extension("sqlite")
        self._attach(
            f"ATTACH {_quote('ducklake:sqlite:' + Path(location.path).as_posix())} AS {LAKE} "
            f"({', '.join(options)})"
        )

    def attach_postgres(
        self, location: PostgresLakeLocation, password: str, attach_options: Sequence[str]
    ) -> None:
        """Attach a lake whose catalog is in PostgreSQL; the password comes from the environment."""
        self._load_extension("postgres")
        try:
            # A temporary default secret supplies the password to every PostgreSQL connection of
            # this database; DuckDB never shows secret values (duckdb_secrets() redacts them).
            self._connection.sql(
                f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD {_quote(password)})"
            )
            options = list(attach_options)
            if location.metadata_schema:
                options.append(f"METADATA_SCHEMA {_quote(location.metadata_schema)}")
            connection = _with_root_cert(location.connection)
            self._connection.sql(
                f"ATTACH {_quote('ducklake:postgres:' + connection)} AS {LAKE} "
                f"({', '.join(options)})"
            )
        except duckdb.Error as error:
            # `from None`: the DuckDB exception is not chained, so a logged traceback cannot show
            # anything the redacted message does not.
            raise StoreError(
                "STORE_CATALOG_UNAVAILABLE",
                "The PostgreSQL catalog could not be reached or refused the credentials.",
                {"duckdb": str(error)},
            ) from None

    def _load_extension(self, extension: str) -> None:
        try:
            self._connection.install_extension(extension)
            self._connection.load_extension(extension)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_EXTENSION_UNAVAILABLE",
                f"The DuckDB extension {extension} could not be installed or loaded.",
                {"extension": extension, "duckdb": str(error)},
            ) from error
