"""A disposable PostgreSQL role for the repository's and the server's tests of an access instance
on PostgreSQL (#436): the store refuses a superuser, so each test gets an ordinary role that may
create schemas, dropped with everything it owns afterwards."""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pg8000.native
import pytest
from orvex.contracts.access.values import PostgresLocation


def postgres_connection() -> tuple[str, str]:
    connection = os.environ.get("ACCESS_TEST_POSTGRES", "")
    password = os.environ.get("ACCESS_TEST_POSTGRES_PASSWORD", "")
    if not connection or not password:
        if os.environ.get("CI"):
            pytest.fail("ACCESS_TEST_POSTGRES and ACCESS_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL configured for the access store tests")
    return connection, password


@contextmanager
def postgres_location() -> Iterator[PostgresLocation]:
    """An access store location on a schema of its own, reached as a role of its own."""
    connection, password = postgres_connection()
    keywords = dict(pair.split("=", 1) for pair in connection.split())
    role, secret = f"access_t_{uuid.uuid4().hex[:12]}", secrets.token_hex(16)
    admin = pg8000.native.Connection(
        keywords["user"],
        password=password,
        host=keywords["host"],
        port=int(keywords.get("port", "5432")),
        database=keywords["dbname"],
    )
    try:
        admin.run(f"CREATE ROLE {role} LOGIN PASSWORD '{secret}'")
        admin.run(f"GRANT CREATE ON DATABASE {keywords['dbname']} TO {role}")
        own = " ".join(f"{k}={v}" for k, v in keywords.items() if k != "user") + f" user={role}"
        yield PostgresLocation(own, secret, f"t_{uuid.uuid4().hex[:16]}")
    finally:
        admin.run(f"DROP OWNED BY {role} CASCADE")
        admin.run(f"DROP ROLE {role}")
        admin.close()
