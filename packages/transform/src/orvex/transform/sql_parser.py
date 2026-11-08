"""The one way transform opens a DuckDB connection to parse SQL: no files, no extension loading."""

from __future__ import annotations

import duckdb


def parser() -> duckdb.DuckDBPyConnection:
    """A new in-memory connection that can only parse: external access (files, network) and the
    autoloading of known extensions are off from the moment it opens. The caller closes it."""
    return duckdb.connect(
        config={"enable_external_access": False, "autoload_known_extensions": False}
    )
