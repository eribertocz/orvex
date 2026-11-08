"""DuckDB's reading of SQL text in the store: its tokens by character, its parser's Unicode-space
prepass, and the store's one parser database (no lake, no extension, no file access).
"""

from __future__ import annotations

import threading

import duckdb
from orvex.store.errors import StoreError


def _tokens(sql: str) -> list[tuple[int, duckdb.token_type]]:
    """DuckDB's tokens of `sql`, positioned by character.

    `duckdb.tokenize` gives byte offsets into the UTF-8 text; a non-ASCII character before a
    token would shift every later slice of the Python string (#130).
    """
    tokens = duckdb.tokenize(sql)
    if sql.isascii():
        return tokens
    characters: dict[int, int] = {}
    offset = 0
    for index, character in enumerate(sql):
        characters[offset] = index
        offset += len(character.encode("utf-8"))
    characters[offset] = len(sql)
    return [(characters[position], kind) for position, kind in tokens]


# The characters DuckDB 2.0.0's parser reads as whitespace beyond ASCII (measured over every code
# point); `duckdb.tokenize` keeps them inside the neighboring token (#213).
PARSER_SPACES = (
    "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b"
    "\u202f\u205f\u2060\u3000\ufeff"
)


def _plain(sql: str) -> str:
    """`sql` as DuckDB 2.0.0's parser reads it: every character in `PARSER_SPACES` that its
    prepass (`Parser::StripUnicodeSpaces`) rewrites is written as a space, and only those.

    The prepass decides what is quoted with its own simple tracker, not with the scanner:
    `'…'` and `"…"` with doubled quotes, `$tag$…$tag$`, `--` comments up to a line break, no
    block comments, no backslash escapes, and none of the last two bytes. A copy that used the
    scanner's view (`duckdb.tokenize`) kept a character inside `'…'` after `/*'*/` or an
    escaped quote in `E'…'` that DuckDB then rewrote, so the guard checked another file name
    than DuckDB opened (#213, Reviewer pass 1). This text is the prepass's own fixed point:
    DuckDB changes nothing in it after the checks, so the text checked is the text run.
    """
    if sql.isascii():
        return sql
    try:
        data = sql.encode("utf-8")
    except UnicodeEncodeError as error:
        raise StoreError(
            "STORE_QUERY_FAILED", "The SQL could not be parsed.", {"duckdb": "invalid UTF-8"}
        ) from error
    spaces = _prepass_spaces(data)
    if not spaces:
        return sql
    parts: list[bytes] = []
    previous = 0
    for position, length in spaces:
        parts.append(data[previous:position])
        parts.append(b" ")
        previous = position + length
    parts.append(data[previous:])
    return b"".join(parts).decode("utf-8")


def _tag_start(byte: int) -> bool:
    return 65 <= byte <= 90 or 97 <= byte <= 122 or byte == 95 or byte >= 0x80


def _tag_byte(byte: int) -> bool:
    return _tag_start(byte) or 48 <= byte <= 57


def _prepass_spaces(data: bytes) -> list[tuple[int, int]]:
    """The (byte position, length) of each Unicode space DuckDB 2.0.0's
    `Parser::StripUnicodeSpaces` rewrites, a line-by-line port of its states (`regular`,
    `in_quotes`, `in_dollar_quotes`, `in_comment`), bounds included."""
    size = len(data)
    found: list[tuple[int, int]] = []
    position = 0
    state = "regular"
    quote = 0
    tag = b""
    while state != "end":
        if state == "regular":
            state = "end"
            while position + 2 < size:
                byte, second, third = data[position], data[position + 1], data[position + 2]
                if byte == 0xC2 and second == 0xA0:
                    found.append((position, 2))
                if byte == 0xE2:
                    if (second == 0x80 and (0x80 <= third <= 0x8B or third == 0xAF)) or (
                        second == 0x81 and third in {0x9F, 0xA0}
                    ):
                        found.append((position, 3))
                elif byte == 0xE3:
                    if second == 0x80 and third == 0x80:
                        found.append((position, 3))
                elif byte == 0xEF:
                    if second == 0xBB and third == 0xBF:
                        found.append((position, 3))
                elif byte in {0x22, 0x27}:
                    quote = byte
                    position += 1
                    state = "quotes"
                    break
                elif byte == 0x24 and (second == 0x24 or _tag_start(second)):
                    position += 1
                    start = position
                    state = "end"
                    while position + 2 < size:
                        if data[position] == 0x24:
                            tag = data[start:position]
                            state = "dollar"
                            break
                        if not _tag_byte(data[position]):
                            state = "regular"  # not a tag: read on from this byte
                            break
                        position += 1
                    break
                elif byte == 0x2D and second == 0x2D:
                    state = "comment"
                    break
                position += 1
        elif state == "quotes":
            state = "end"
            while position + 1 < size:
                if data[position] == quote:
                    if data[position + 1] == quote:
                        position += 2  # a doubled quote
                        continue
                    position += 1
                    state = "regular"
                    break
                position += 1
        elif state == "dollar":
            state = "end"
            length = len(tag)
            while position + 2 < size:
                if (
                    data[position] == 0x24
                    and size - (position + 1) >= length + 1
                    and data[position + length + 1] == 0x24
                    and data.startswith(
                        tag, position + 1
                    )  # stops at the first difference, as memcmp
                ):
                    position += length + 1  # on the closing `$`, read again as regular
                    state = "regular"
                    break
                position += 1
        else:  # comment
            state = "end"
            while position < size:
                if data[position] in {0x0A, 0x0D}:
                    state = "regular"
                    break
                position += 1
    return found


_PARSER_LOCK = threading.Lock()
_PARSER_DATABASE: list[duckdb.DuckDBPyConnection] = []


def _parser() -> duckdb.DuckDBPyConnection:
    """A cursor on the process's one in-memory DuckDB database for parsing only: no lake, no
    extension, no file access. One database for every thread (a cursor each call), so parsing
    adds no DuckDB instance per thread (#160, Reviewer pass 1). The caller closes the cursor."""
    with _PARSER_LOCK:
        if not _PARSER_DATABASE:
            database = duckdb.connect(config={"enable_external_access": False})
            database.execute("SET autoload_known_extensions = false")
            _PARSER_DATABASE.append(database)
        return _PARSER_DATABASE[0].cursor()
