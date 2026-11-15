"""The servers some cases of the store's kit need, taken from the environment as the store's own
tests take them: a ClickHouse administrator of a disposable server (`STORE_TEST_CLICKHOUSE`,
`host:port:user`, and `STORE_TEST_CLICKHOUSE_PASSWORD`). Where none is given, a case that needs
one fails (KIT_SERVICE_MISSING); it never skips.
"""

from __future__ import annotations

import os
import secrets
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from orvex.contracts.store.interface import REQUIRED_SETTINGS
from orvex.contracts.store.values import ClickHouseTarget

CLICKHOUSE = "STORE_TEST_CLICKHOUSE"
CLICKHOUSE_PASSWORD = "STORE_TEST_CLICKHOUSE_PASSWORD"
READER_PASSWORD = "STORE_KIT_CLICKHOUSE_READER_PASSWORD"  # set for each reader, never stored
SAFE = {
    "readonly": "1",
    **REQUIRED_SETTINGS,
    "max_execution_time": "30",
    "max_memory_usage": "1000000000",
}


@dataclass(frozen=True, slots=True)
class ClickHouseServer:
    host: str
    port: int
    user: str
    password: str

    def run(self, sql: str) -> str:
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/",
            data=sql.encode(),
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
        return body.decode()

    def target(self, user: str, password_env: str, database: str = "default") -> ClickHouseTarget:
        return ClickHouseTarget(self.host, self.port, database, user, password_env, tls=False)

    def reader(self, database: str, settings: Mapping[str, str] = SAFE) -> ClickHouseTarget:
        """A new user of `database` whose profile has `settings`, its password in the
        environment."""
        name = f"kit_{secrets.token_hex(6)}"
        password = secrets.token_hex(16)
        listed = ", ".join(f"{key} = '{value}'" for key, value in settings.items())
        self.run(f"CREATE SETTINGS PROFILE {name} SETTINGS {listed}")
        self.run(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
        self.run(f"GRANT SELECT ON {database}.* TO {name}")
        os.environ[READER_PASSWORD] = password
        return self.target(name, READER_PASSWORD, database)

    def database(self, *tables: str) -> str:
        """A new database with `tables` (`name (columns) ENGINE = …` each)."""
        name = f"kit_{secrets.token_hex(6)}"
        self.run(f"CREATE DATABASE {name}")
        for table in tables:
            self.run(f"CREATE TABLE {name}.{table}")
        return name


def clickhouse() -> ClickHouseServer:
    named = os.environ.get(CLICKHOUSE)
    if not named:
        raise AssertionError(
            f"KIT_SERVICE_MISSING: this case needs a ClickHouse server ({CLICKHOUSE})"
        )
    host, port, user = named.split(":")
    return ClickHouseServer(host, int(port), user, os.environ.get(CLICKHOUSE_PASSWORD, ""))
