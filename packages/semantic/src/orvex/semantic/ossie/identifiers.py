"""Ossie identifiers (`expression_language.md` L86-L88): at most 128 characters; an unquoted
identifier is case-insensitive (normalized to upper case), a double-quoted one is compared
exactly. Names written in the document follow the same rule."""

from __future__ import annotations

import re

from orvex.semantic.ossie.sql import MAX_IDENTIFIER, normalize

__all__ = ["MAX_IDENTIFIER", "identifier_length", "is_valid_identifier", "normalize"]

_REGULAR = re.compile(r"[^\W\d]\w*")


def identifier_length(name: str) -> int:
    return len(normalize(name))


def is_valid_identifier(name: str) -> bool:
    """L86: identifiers follow ANSI SQL naming. A regular identifier starts with a letter or `_`
    and continues with letters, digits or `_`; a double-quoted identifier holds any non-empty
    text, with `""` for a quote."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return bool(normalize(name)) and '"' not in name[1:-1].replace('""', "")
    return _REGULAR.fullmatch(name) is not None
