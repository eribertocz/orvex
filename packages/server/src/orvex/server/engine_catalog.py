"""The project's Direct Query engines, from `connections.yaml` (P2-29, #370; P2-30 PR 3, #399).

Every `ducklake` connection is an engine entry of route `existing_ducklake`: its environment is
the catalog's identity and the data location, never a secret, so two connections to one lake
are one engine and a question across them stays on it. The dialect is DuckDB SQL, the managed
lake's, so the entry offers every capability but detail pages (whose cursor names a managed-lake
snapshot). Every `clickhouse` connection is an entry of route `live` (P2-31 PR 4, #225): its
environment is the server's host, port and database, its dialect ClickHouse SQL, and it offers
every capability its adapter runs. Every other connection (Route A sources) is listed apart: a
dataset naming one is the model's problem.

An entry says only where a dataset lives. Whether its engine runs in this process is the
development switch's decision (`development_switch`), and its adapter is supplied apart.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from orvex.contracts.project.v1 import (
    ClickhouseConnection,
    DucklakeConnection,
    DucklakePostgresCatalog,
)
from orvex.semantic.adapters.clickhouse import CAPABILITIES as CLICKHOUSE_CAPABILITIES
from orvex.semantic.engines import CAPABILITIES, EngineCatalog, EngineEntry, Route

KIND = "ducklake"
ROUTE: Route = "existing_ducklake"
EXISTING_DUCKLAKE_CAPABILITIES = frozenset(CAPABILITIES - {"detail_pages"})


def environment(connection: DucklakeConnection) -> str:
    """The lake's identity as an engine: its catalog and its data location."""
    catalog = connection.catalog.root
    if isinstance(catalog, DucklakePostgresCatalog):
        where = (
            f"postgres://{catalog.host.lower()}:{catalog.port}/{catalog.database}"
            f"/{catalog.schema_ or 'public'}"
        )
    else:
        where = f"sqlite:{catalog.path.root}"
    return f"{where}|{connection.data_path}"


def live_environment(connection: ClickhouseConnection) -> str:
    """A ClickHouse engine's identity: its server and database, never its user or secret."""
    return f"{connection.host.lower()}:{connection.port}/{connection.database}"


def engine_catalog(connections: Mapping[str, object] | Collection[str]) -> EngineCatalog:
    """The engine entry of every existing-DuckLake and ClickHouse connection; the other names
    apart."""
    if not isinstance(connections, Mapping):
        return EngineCatalog({}, frozenset(connections))
    entries = {
        name: EngineEntry(name, KIND, ROUTE, environment(c), EXISTING_DUCKLAKE_CAPABILITIES)
        for name, c in connections.items()
        if isinstance(c, DucklakeConnection)
    }
    entries |= {
        name: EngineEntry(name, "clickhouse", "live", live_environment(c), CLICKHOUSE_CAPABILITIES)
        for name, c in connections.items()
        if isinstance(c, ClickhouseConnection)
    }
    return EngineCatalog(entries, frozenset(connections) - set(entries))
