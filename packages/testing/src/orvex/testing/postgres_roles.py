"""A disposable PostgreSQL role for the tests of an access store on PostgreSQL (#436): the store
refuses a superuser, so each test gets an ordinary role that may create schemas, dropped with
everything it owns afterwards; and the superuser connection that plants what the store itself
must never write."""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pg8000.native
import pytest


def postgres_connection() -> tuple[str, str]:
    connection = os.environ.get("ACCESS_TEST_POSTGRES", "")
    password = os.environ.get("ACCESS_TEST_POSTGRES_PASSWORD", "")
    if not connection or not password:
        if os.environ.get("CI"):
            pytest.fail("ACCESS_TEST_POSTGRES and ACCESS_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL configured for the access store tests")
    return connection, password


@contextmanager
def access_role(connection: str, password: str) -> Iterator[tuple[str, str]]:
    """An ordinary role for one test, as the store requires (it refuses a superuser): it may
    create schemas in the database, and everything it owns is dropped with it afterwards. Yields
    its connection string and password."""
    role, secret = f"access_t_{uuid.uuid4().hex[:12]}", secrets.token_hex(16)
    keywords = dict(pair.split("=", 1) for pair in connection.split())
    admin = superuser(connection, password)
    try:
        admin.run(f"CREATE ROLE {role} LOGIN PASSWORD '{secret}'")
        admin.run(f"GRANT CREATE ON DATABASE {keywords['dbname']} TO {role}")
        yield (
            " ".join(f"{k}={v}" for k, v in keywords.items() if k != "user") + f" user={role}",
            secret,
        )
    finally:
        admin.run(f"DROP OWNED BY {role} CASCADE")
        admin.run(f"DROP ROLE {role}")
        admin.close()


def superuser(connection: str, password: str) -> pg8000.native.Connection:
    """A direct connection, for what the store itself must never do: drop a test's schema,
    plant a bad row, create another role."""
    keywords = dict(pair.split("=", 1) for pair in connection.split())
    return pg8000.native.Connection(
        keywords["user"],
        password=password,
        host=keywords["host"],
        port=int(keywords.get("port", "5432")),
        database=keywords["dbname"],
    )
