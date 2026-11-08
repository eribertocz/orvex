"""A PostgreSQL source's credentials: its password from the environment, registered as a secret,
and its TLS settings (required off this machine) as libpq keywords.
"""

from __future__ import annotations

import os
import re

from orvex.contracts.store.v1 import PostgresSourceAttachment
from orvex.store.errors import StoreError, register_secret
from orvex.store.locations import _local_connection

_VARIABLE = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")


def _source_password(source: PostgresSourceAttachment) -> str:
    _require_source_tls(source)
    _source_tls(source)  # an unset CA variable is refused before anything is opened
    password = os.environ.get(source.password_env, "")
    if not password:
        raise StoreError(
            "STORE_SECRET_MISSING",
            f"The environment variable {source.password_env} is not set.",
            {"variable": source.password_env, "source": source.name},
        )
    register_secret(password)
    return password


def _require_source_tls(source: PostgresSourceAttachment) -> None:
    """Refuse a PostgreSQL source on another machine unless it is reached with
    `sslmode=verify-full` (#122): the password and the production rows would otherwise travel
    over a connection that does not prove the server is the named host. The same rule as a
    catalog's (`_local_connection`), environment included."""
    if _local_connection({"host": source.host}) or source.sslmode == "verify-full":
        return
    raise StoreError(
        "STORE_TLS_REQUIRED",
        f"Source {source.name} is on another machine and must be reached with "
        "sslmode=verify-full (TLS that checks the server's certificate and name).",
        {"source": source.name, "host": source.host, "sslmode": source.sslmode or "(none)"},
    )


def _source_tls(source: PostgresSourceAttachment) -> str:
    """The source's TLS settings as libpq connection keywords (quoted values)."""
    if source.sslmode is None:
        return ""
    settings = f" sslmode={_libpq_value(source.sslmode)}"
    if source.sslmode == "verify-full":
        root = source.sslrootcert or "system"
        if root != "system":
            match = _VARIABLE.fullmatch(root)
            assert match is not None  # the contract's pattern
            variable = match.group(1)
            root = os.environ.get(variable, "")
            if not root:
                raise StoreError(
                    "STORE_SECRET_MISSING",
                    f"The environment variable {variable} is not set (the certificate "
                    f"authorities of source {source.name}).",
                    {"variable": variable, "source": source.name},
                )
        settings += f" sslrootcert={_libpq_value(root)}"
    return settings


def _libpq_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
