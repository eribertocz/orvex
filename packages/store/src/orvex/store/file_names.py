"""The statement check that no file is named in SQL (#136, #178, #193): quoted strings and dotted
names that name a file, match files through a pattern within a budget, or are object-storage
addresses.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import re
from collections.abc import Iterator

import duckdb
from orvex.store.errors import StoreError
from orvex.store.sql_parser import _parser

_GLOB = re.compile(r"[*?\[]")
# Object-storage addresses DuckDB opens as files (httpfs, azure, Hugging Face). A statement never
# names one: only DuckLake reaches the lake's objects, and the sandbox checks these paths as text
# only (`..` is resolved after the check, #140).
_OBJECT_ADDRESS = re.compile(r"^\s*(s3|s3a|s3n|gs|gcs|r2|az|azure|abfs|abfss|hf)://", re.IGNORECASE)


def _refuse_file_names(
    sql: str, tokens: list[tuple[int, duckdb.token_type]], ends: dict[int, int]
) -> None:
    """Refuse a quoted string or quoted name that names an existing file, matches files, or is an
    object-storage address (`s3://…`, `gs://…`, `az://…`, …).

    DuckDB reads such a string as a file in many places (`FROM '…'`, `MERGE … USING '…'`,
    `PIVOT '…'`, `SUMMARIZE '…'`, …); a model reaches a file only through its source name. Every
    quoting form counts (`'…'`, `E'…'`, `$$…$$`, `$tag$…$tag$`, `"…"`). The file sandbox, which
    allows exactly the declared files, is the boundary behind this check.
    """

    def refused(reason: str) -> StoreError:
        # `reason` tells a named file from a pattern that could not be checked (#193).
        return StoreError(
            "STORE_STATEMENT_INVALID",
            "A file cannot be named in a statement; declare it as a source and use its name.",
            {"reason": reason},
        )

    budget = _Budget()  # one for the whole statement, so many patterns cannot add up (#178)
    texts: list[tuple[str, duckdb.token_type]] = []
    for position, kind in tokens:
        end = ends[position]
        text = sql[position:end].strip()
        texts.append((text, kind))
        quoted = kind == duckdb.token_type.string_const or text.startswith('"')
        if quoted and (reason := _names_a_file(text, budget)) is not None:
            raise refused(reason)
    # A dotted name: DuckDB joins its parts with `.` and reads the result as a file when no
    # table has that name (`"<dir>/x".parquet`, `x.parquet`, `"<dir>/*".csv`; measured on
    # 2.0.0), so every chain of name parts is checked as one path too (#136, Reviewer pass 6).
    names = (duckdb.token_type.identifier, duckdb.token_type.keyword)
    index = 0
    while index < len(texts):
        if texts[index][1] not in names:
            index += 1
            continue
        parts = [_string_content(texts[index][0])]
        index += 1
        while index + 1 < len(texts) and texts[index][0] == "." and texts[index + 1][1] in names:
            parts.append(_string_content(texts[index + 1][0]))
            index += 2
        if len(parts) > 1 and (reason := _names_a_file(".".join(parts), budget)) is not None:
            raise refused(reason)


# A dollar-quoted constant; DuckDB, like PostgreSQL, accepts any tag without `$` or white space
# (non-ASCII letters included), so every such tag is read here (#153).
_DOLLAR_QUOTED = re.compile(r"\$(?P<tag>[^$\s]*)\$(?P<body>.*)\$(?P=tag)\$", re.DOTALL)


def _string_content(token: str) -> str:
    """The text a quoted token stands for: `'…'` (with `''`), `E'…'`, `$$…$$`, `$tag$…$tag$` and
    quoted names `"…"` (with `""`). An unquoted token is returned as it is."""
    dollar = _DOLLAR_QUOTED.fullmatch(token)
    if dollar is not None:
        return dollar["body"]
    quote = token[-1:]
    if quote not in {"'", '"'}:
        return token
    if token[:1] in {"e", "E"} and token[1:2] == "'":
        return _escape_string_value(token)
    start = token.find(quote)  # after a prefix, if any
    return token[start + 1 : -1].replace(quote * 2, quote)


def _escape_string_value(token: str) -> str:
    """The value of an `E'…'` constant, read by DuckDB itself so that every escape it accepts
    (`\\x2e`, octal, `\\.` for `.`, …) is decoded exactly as it would be. The token must be
    one string constant on its own; the connection has no external access."""
    tokens = duckdb.tokenize(token)
    if len(tokens) != 1 or tokens[0][1] != duckdb.token_type.string_const:
        return token[2:-1]
    parser = _parser()  # one parser database for the process, not a connection each (#178)
    try:
        row = parser.execute(f"SELECT {token}").fetchone()
    except duckdb.Error:
        # Not a valid constant (`E'a\\'`, `E'\\x00'`): the statement fails later, when it is
        # parsed, with `STORE_QUERY_FAILED`.
        return token[2:-1]
    finally:
        parser.close()
    return str(row[0]) if row is not None and row[0] is not None else ""


# How many directory entries the file checks of one statement may read (#178): a pattern too
# broad to check within it counts as naming a file, so the statement is refused.
PATTERN_BUDGET = 10_000

# What reading one folder costs, in entries: far more than an entry when the disk cache is
# cold (the CI runner took about 2 ms per folder of /usr, so 455 folders took 1 s), so at
# most 200 folders are read for one statement whatever they hold. A folder is read once per
# statement: a pattern that lists it again reads the kept listing, at no cost (#193).
FOLDER_COST = 50

# How many steps the walks of one statement may take together: every place a walk visits and
# every entry it goes through, from a kept listing too. It bounds the work done without reading
# the disk, so patterns over folders already read cannot add up without limit (#193).
STEP_BUDGET = 100_000


class _Budget:
    """What a statement's file checks may still read, and the folders they have read (#193):
    each folder is read once per statement, then its listing is kept for the other patterns.
    One `_Budget` belongs to one statement's check, never shared between statements."""

    def __init__(self, entries: int = PATTERN_BUDGET, steps: int = STEP_BUDGET) -> None:
        self.entries = entries
        self.steps = steps
        self.listings: dict[str, list[tuple[str, bool]]] = {}


class _Unchecked(Exception):
    """A pattern could not be checked: too broad for the budget (`reason` `pattern_too_broad`),
    or it meets a folder that cannot be listed (`pattern_unreadable`)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _spend(budget: _Budget, units: int = 1) -> None:
    budget.entries -= units
    if budget.entries < 0:
        raise _Unchecked("pattern_too_broad")


def _step(budget: _Budget) -> None:
    budget.steps -= 1
    if budget.steps < 0:
        raise _Unchecked("pattern_too_broad")


def _listed(folder: str, budget: _Budget) -> list[tuple[str, bool]]:
    """A folder's entries as (name, is a folder). The first read of a folder in a statement is
    counted against the budget, the read itself as one entry, or as `FOLDER_COST` when the
    folder is there to be read, and each entry as one; later reads use the kept listing (#193).
    A folder that does not exist, is not one, or whose name is too long to exist has none; one
    that cannot be listed, or an entry whose kind cannot be told (a symbolic-link loop), cannot
    be checked."""
    key = folder or "."
    kept = budget.listings.get(key)
    if kept is not None:
        return kept
    _spend(budget)
    entries: list[tuple[str, bool]] = []
    try:
        with os.scandir(key) as found:
            _spend(budget, FOLDER_COST - 1)  # a folder that is there and is read
            for entry in found:
                _spend(budget)
                entries.append((entry.name, entry.is_dir()))
    except FileNotFoundError, NotADirectoryError:
        entries = []
    except OSError as error:
        # A name too long for the file system names no file DuckDB could open: ordinary text
        # such as "… and/or estimated?" (#178, Reviewer pass 3).
        if error.errno != errno.ENAMETOOLONG:
            raise _Unchecked("pattern_unreadable") from None
        entries = []
    budget.listings[key] = entries
    return entries


def _expanded(pattern: str, budget: _Budget) -> Iterator[str]:
    """The paths a glob pattern names, as DuckDB expands it (`**` at any depth, hidden names
    included, like `glob.iglob(recursive=True, include_hidden=True)`), read within `budget`.
    The walk keeps its own stack (no recursion, whatever the pattern's length), merges a run of
    `**` into one, drops empty parts, and counts every step against the statement's steps and
    every folder it reads against its entries (#178, Reviewer pass 1; #193)."""
    parts: list[str] = []
    for part in pattern.split(os.sep):
        if part and not (part == "**" and parts[-1:] == ["**"]):
            parts.append(part)
    stack: list[tuple[str, int]] = [(os.sep if pattern.startswith(os.sep) else "", 0)]
    while stack:
        _step(budget)
        base, index = stack.pop()
        if index == len(parts):
            yield base
            continue
        head = parts[index]
        if head == "**":
            stack.append((base, index + 1))  # no folder at all
            for name, folder in _listed(base, budget):
                _step(budget)  # each entry, from a kept listing too (#193, Reviewer pass 1)
                below = os.path.join(base, name)
                if index + 1 == len(parts):
                    yield below
                if folder:
                    stack.append((below, index))
        elif _GLOB.search(head):
            for name, _ in _listed(base, budget):
                _step(budget)
                if fnmatch.fnmatchcase(name, head):
                    stack.append((os.path.join(base, name), index + 1))
        else:
            stack.append((os.path.join(base, head), index + 1))


def _names_a_file(token: str, budget: _Budget | None = None) -> str | None:
    """Why `token` counts as naming a file (#193): `names_a_file` when it is an object-storage
    address, a `file:` URI, an existing file or a pattern that matches one;
    `pattern_too_broad` or `pattern_unreadable` when a pattern could not be checked. None when
    it names no file."""
    budget = budget if budget is not None else _Budget()
    content = _string_content(token)
    if _OBJECT_ADDRESS.match(content):
        return "names_a_file"
    if content.startswith("file:/"):
        # DuckDB opens `file:/p`, `file:///p` and `file://localhost/p` as the local file `/p`
        # (measured on 2.0.0), whether or not it exists yet (#136, Reviewer pass 5).
        return "names_a_file"
    candidates = {content}
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return "names_a_file"
        looks_like_path = any(mark in candidate for mark in ("/", "\\", "."))
        if looks_like_path and _GLOB.search(candidate):
            try:
                # As DuckDB expands it: `**` at any depth, hidden names included (#136, Reviewer
                # pass 7), within the statement's budget (#178).
                if any(os.path.isfile(match) for match in _expanded(path, budget)):
                    return "names_a_file"
            except _Unchecked as unchecked:
                return unchecked.reason  # too broad, or a folder that cannot be listed: refused
    return None
