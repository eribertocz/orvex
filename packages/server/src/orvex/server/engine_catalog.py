"""The project's Direct Query engines, from `connections.yaml` (P2-29, #370; P2-30 PR 3, #399).

Every `ducklake` connection is an engine entry of route `existing_ducklake`: its environment is
the catalog's identity and the data location, never a secret, so two connections to one lake
are one engine and a question across them stays on it. The dialect is DuckDB SQL, the managed
lake's, so the entry offers every capability but detail pages (whose cursor names a managed-lake
snapshot). Every other connection (Route A sources, and ClickHouse until its engine is wired,
#225) is listed apart: a dataset naming one is the model's problem.

An entry says only where a dataset lives. Whether its engine runs in this process is the
development switch's decision (`development_switch`), and its adapter is supplied apart.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from orvex.contracts.project.v1 import DucklakeConnection, DucklakePostgresCatalog
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


def engine_catalog(connections: Mapping[str, object] | Collection[str]) -> EngineCatalog:
    """The engine entry of every existing-DuckLake connection; the other names apart."""
    if not isinstance(connections, Mapping):
        return EngineCatalog({}, frozenset(connections))
    entries = {
        name: EngineEntry(name, KIND, ROUTE, environment(c), EXISTING_DUCKLAKE_CAPABILITIES)
        for name, c in connections.items()
        if isinstance(c, DucklakeConnection)
    }
    return EngineCatalog(entries, frozenset(connections) - set(entries))
