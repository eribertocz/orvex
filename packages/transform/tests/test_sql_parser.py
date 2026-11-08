"""#246: transform parses SQL only on connections with no files and no extension loading."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest
from orvex.transform import auto_filter, graph, incremental, lineage, reconciliation
from orvex.transform.models import Model, discover_models
from orvex.transform.sql_parser import parser

APPEND = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
)
SETTINGS = (
    "SELECT current_setting('enable_external_access'), current_setting('autoload_known_extensions')"
)


def test_the_parser_connection_reads_no_files_and_loads_no_extension() -> None:
    connection = parser()
    try:
        assert connection.execute(SETTINGS).fetchone() == (False, False)
    finally:
        connection.close()


class _Spy:
    """A connection that records its settings each time it is asked to parse SQL."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, seen: list[object]) -> None:
        self._connection = connection
        self._seen = seen

    def _record(self) -> None:
        self._seen.append(self._connection.execute(SETTINGS).fetchone())

    def execute(self, sql: str, *arguments: object, **options: object) -> duckdb.DuckDBPyConnection:
        if "json_serialize_sql" in sql:
            self._record()
        return self._connection.execute(sql, *arguments, **options)

    def extract_statements(self, sql: str) -> list[duckdb.Statement]:
        self._record()
        return self._connection.extract_statements(sql)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def _model(tmp_path: Path) -> Model:
    folder = tmp_path / "models" / "s"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.sql").write_text("SELECT * FROM t WHERE read_at > $since", encoding="utf-8")
    (folder / "m.yaml").write_text(APPEND, encoding="utf-8")
    found = discover_models(tmp_path, {})
    assert found.problems == ()
    return found.models[0]


PARSES: dict[str, Callable[[Path], object]] = {
    "reconciliation.expression": lambda _: reconciliation.expression("1 + 1"),
    "incremental.uses_since": lambda _: incremental.uses_since("SELECT $since"),
    "incremental.since_is_a_lower_bound": lambda _: incremental.since_is_a_lower_bound(
        "SELECT * FROM t WHERE read_at > $since", "read_at"
    ),
    "incremental.sql_depth": lambda _: incremental.sql_depth("SELECT 1"),
    "incremental.append_rows_problem": lambda tmp: incremental.append_rows_problem(_model(tmp)),
    "models.discover_models": _model,
    "graph.known_output_names": lambda _: graph.known_output_names("SELECT 1 AS a"),
    "auto_filter.filtered_sql": lambda _: auto_filter.filtered_sql(
        "SELECT * FROM t", "read_at", auto_filter.known({"t": ["read_at"]})
    ),
    "lineage.inputs": lambda tmp: lineage.inputs(_model(tmp), {}, {}, "lake"),
}


@pytest.mark.parametrize("parse", sorted(PARSES))
def test_each_parse_runs_on_a_connection_with_no_files_and_no_extension_loading(
    parse: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = duckdb.connect
    seen: list[object] = []
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _Spy(connect(*a, **k), seen))
    PARSES[parse](tmp_path)
    assert seen, "no SQL was parsed on a connection of its own"
    assert set(seen) == {(False, False)}
