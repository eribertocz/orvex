"""Text of DuckDB SQL: string literals and quoted identifiers."""

from __future__ import annotations


def literal(text: str) -> str:
    """`text` as a SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


def identifier(name: str) -> str:
    """`name` as a quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'
