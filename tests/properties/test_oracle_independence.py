"""P2-28 (#367; Owner correction 1): the reference evaluator shares nothing with the engine.
Its module imports nothing from the product: only the standard library (and DuckDB, inside
the second oracle's own function)."""

from __future__ import annotations

import ast
from pathlib import Path

ORACLE = Path(__file__).with_name("oracle.py")
ALLOWED = {
    "__future__",
    "collections",
    "datetime",
    "decimal",
    "typing",
    "zoneinfo",
    "duckdb",  # the second oracle's engine, with SQL written in this file
}


def test_the_oracle_imports_nothing_from_the_product() -> None:
    tree = ast.parse(ORACLE.read_text("utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= ALLOWED, imported - ALLOWED
    assert "orvex" not in imported
    assert "conformance" not in imported
    assert "tests" not in imported
