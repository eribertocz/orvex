"""Temporary: show plan shapes in CI."""
from __future__ import annotations

import json

import pytest
from orvex.store.lake import open_lake

from test_database_sources import LIMITS, postgres_source, sqlite_source  # type: ignore[import-not-found]


def test_show_plans(location, schema, sqlite_file) -> None:  # type: ignore[no-untyped-def]
    lake = open_lake(location, LIMITS, [postgres_source(None), sqlite_source(sqlite_file)])
    out = []
    for sql in [f"SELECT loose FROM production.{schema}.readings", "SELECT * FROM field.billed",
                "SELECT amount FROM field.charges"]:
        row = lake._connection.execute(f"EXPLAIN (FORMAT json) {sql}").fetchone()
        out.append(json.dumps(json.loads(row[1]))[:1500])
    out.append(repr(sorted(lake._inexact)))
    lake.close()
    pytest.fail("\n\n".join(out))
