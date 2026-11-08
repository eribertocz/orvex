"""Names in ClickHouse statements: a quoted name reads a backslash as an escape, so it is
escaped too."""

from __future__ import annotations


def quote_name(name: str) -> str:
    """A name from the engine's catalog, quoted for ClickHouse."""
    return '"' + name.replace("\\", "\\\\").replace('"', '""') + '"'


def unquote_name(written: str) -> str:
    """The name a quoted (or bare) word of a statement stands for."""
    if written.startswith('"'):
        return written[1:-1].replace('""', '"').replace("\\\\", "\\")
    return written
