"""Read an Ossie document's text into plain values, safely, for YAML and JSON alike.

Ossie defines a model as one JSON or YAML document (`spec.md` L85). Only this module knows the
formats: both parsers return the same plain values (dict, list, str, int, Decimal, bool, None,
and the dates and times YAML itself reads) and the line of every value, and everything after
them is format-independent.

Refused in both formats: more than one document, duplicate keys, keys that are not text, nesting
deeper than `MAX_DEPTH`, and numbers that are not finite. No number becomes a float: an integral
number is an `int` (a `Decimal` beyond Python's integer-string limit), any other a `Decimal` with
the digits as written. YAML also refuses anchors, aliases, merge keys and tags outside YAML's
core schema; JSON follows RFC 8259 strictly (no byte-order mark, comments, `NaN`, `Infinity`,
trailing commas or text after the value).

No error carries text from the document: problems are located by the path of keys and indexes
(made safe by the reader) and a line.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

import yaml

Format = Literal["yaml", "json"]
MAX_DEPTH = 32
MAX_BYTES = 8 * 1024 * 1024
# Python refuses to build an int from more digits than this (sys.int_info).
_INT_DIGITS = 4300
_CORE_TAGS = frozenset(
    "tag:yaml.org,2002:" + name
    for name in ("str", "int", "float", "bool", "null", "seq", "map", "timestamp")
)
_YAML_DECIMAL = re.compile(r"[-+]?(?:[0-9][0-9_]*)?(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?")
_YAML_INTEGER = re.compile(r"[-+]?[0-9][0-9_]*")

Path = tuple[str | int, ...]


class ParseProblem(Exception):
    """A document that cannot be read. `path` is the place (keys and indexes), `line` 1-based."""

    def __init__(self, reason: str, path: Path = (), line: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path
        self.line = line


@dataclass(frozen=True)
class Parsed:
    """The plain values of a document, and the line where each value is written."""

    data: object
    lines: dict[Path, int]


def parse(text: str, format: Format) -> Parsed:  # noqa: A002 - the word the spec uses
    if format == "json":
        return _Json(text).document()
    return _parse_yaml(text)


# --- JSON (RFC 8259) --------------------------------------------------------------------------

_WHITESPACE = " \t\n\r"
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?")


class _Json:
    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0
        self.newlines = [index for index, char in enumerate(text) if char == "\n"]
        self.lines: dict[Path, int] = {}

    def line(self, at: int | None = None) -> int:
        return bisect.bisect_left(self.newlines, self.at if at is None else at) + 1

    def fail(self, reason: str, path: Path) -> ParseProblem:
        return ParseProblem(reason, path, self.line())

    def document(self) -> Parsed:
        if self.text.startswith("﻿"):
            raise self.fail("a JSON document starts without a byte-order mark", ())
        self.skip()
        if self.at == len(self.text):
            raise self.fail("the document is empty", ())
        data = self.value((), 0)
        self.skip()
        if self.at != len(self.text):
            raise self.fail("text follows the JSON value", ())
        return Parsed(data, self.lines)

    def skip(self) -> None:
        while self.at < len(self.text) and self.text[self.at] in _WHITESPACE:
            self.at += 1

    def value(self, path: Path, depth: int) -> object:
        if depth > MAX_DEPTH:
            raise self.fail(f"values are nested more than {MAX_DEPTH} levels deep", path)
        self.lines[path] = self.line()
        char = self.text[self.at] if self.at < len(self.text) else ""
        if char == "{":
            return self.object(path, depth)
        if char == "[":
            return self.array(path, depth)
        if char == '"':
            return self.string(path)
        for word, constant in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(word, self.at):
                self.at += len(word)
                return constant
        match = _NUMBER.match(self.text, self.at)
        if match is None:
            # NaN, Infinity, a bare word or a stray character.
            raise self.fail("not a JSON value", path)
        self.at = match.end()
        written = match.group()
        if match.group(1) is None and match.group(2) is None:
            if len(written.lstrip("-")) <= _INT_DIGITS:
                return int(written)
        return Decimal(written)  # exact, as written; never a float

    def object(self, path: Path, depth: int) -> dict[str, object]:
        self.at += 1
        members: dict[str, object] = {}
        self.skip()
        if self.text.startswith("}", self.at):
            self.at += 1
            return members
        while True:
            self.skip()
            if not self.text.startswith('"', self.at):
                raise self.fail("an object key is a string", path)
            key = self.string(path)
            if key in members:
                raise self.fail("duplicate key", path)
            self.skip()
            if not self.text.startswith(":", self.at):
                raise self.fail("a ':' follows an object key", path)
            self.at += 1
            self.skip()
            members[key] = self.value((*path, key), depth + 1)
            self.skip()
            if self.text.startswith(",", self.at):
                self.at += 1
                continue
            if self.text.startswith("}", self.at):
                self.at += 1
                return members
            raise self.fail("an object continues with ',' or ends with '}'", path)

    def array(self, path: Path, depth: int) -> list[object]:
        self.at += 1
        items: list[object] = []
        self.skip()
        if self.text.startswith("]", self.at):
            self.at += 1
            return items
        while True:
            self.skip()
            items.append(self.value((*path, len(items)), depth + 1))
            self.skip()
            if self.text.startswith(",", self.at):
                self.at += 1
                continue
            if self.text.startswith("]", self.at):
                self.at += 1
                return items
            raise self.fail("an array continues with ',' or ends with ']'", path)

    def string(self, path: Path) -> str:
        self.at += 1
        parts: list[str] = []
        while True:
            if self.at >= len(self.text):
                raise self.fail("a string is not closed", path)
            char = self.text[self.at]
            if char == '"':
                self.at += 1
                return "".join(parts)
            if char < " ":
                raise self.fail("a control character inside a string is escaped", path)
            if char != "\\":
                parts.append(char)
                self.at += 1
                continue
            escape = self.text[self.at + 1 : self.at + 2]
            if escape in _ESCAPES:
                parts.append(_ESCAPES[escape])
                self.at += 2
                continue
            if escape != "u":
                raise self.fail("an unknown escape in a string", path)
            code = self.hex4(path)
            if 0xD800 <= code <= 0xDBFF and self.text.startswith("\\u", self.at):
                low = self.hex4(path)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise self.fail("a lone surrogate in a string", path)
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
            elif 0xD800 <= code <= 0xDFFF:
                raise self.fail("a lone surrogate in a string", path)
            parts.append(chr(code))

    def hex4(self, path: Path) -> int:
        digits = self.text[self.at + 2 : self.at + 6]
        if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise self.fail("a \\u escape has four hexadecimal digits", path)
        self.at += 6
        return int(digits, 16)


# --- YAML -------------------------------------------------------------------------------------


def _parse_yaml(text: str) -> Parsed:
    _refuse_anchors(text)
    try:
        documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        raise ParseProblem(
            "invalid YAML", (), None if mark is None else mark.line + 1
        ) from None
    except yaml.YAMLError:
        raise ParseProblem("invalid YAML") from None
    except RecursionError:
        raise ParseProblem(f"values are nested more than {MAX_DEPTH} levels deep") from None
    if len(documents) != 1:
        raise ParseProblem("a file holds exactly one document")
    lines: dict[Path, int] = {}
    loader = yaml.SafeLoader("")
    try:
        data = _yaml_value(documents[0], (), 0, lines, loader)
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]
    return Parsed(data, lines)


def _refuse_anchors(text: str) -> None:
    """Anchors and aliases repeat a value at another place, and nested aliases can expand
    exponentially; they are refused before any node is built."""
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                line = None if event.start_mark is None else event.start_mark.line + 1
                raise ParseProblem("anchors and aliases (&, *) are not supported", (), line)
    except yaml.YAMLError:
        return  # the composer reports it, with its line


def _yaml_value(
    node: yaml.Node, path: Path, depth: int, lines: dict[Path, int], loader: yaml.SafeLoader
) -> object:
    line = node.start_mark.line + 1
    lines.setdefault(path, line)
    if depth > MAX_DEPTH:
        raise ParseProblem(f"values are nested more than {MAX_DEPTH} levels deep", path, line)
    if node.tag not in _CORE_TAGS:
        raise ParseProblem("a tag outside YAML's core schema", path, line)
    if isinstance(node, yaml.MappingNode):
        members: dict[str, object] = {}
        for key, value in node.value:
            key_line = key.start_mark.line + 1
            if key.tag == "tag:yaml.org,2002:merge":
                raise ParseProblem("merge keys (<<) are not supported", path, key_line)
            if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                raise ParseProblem("a key is text", path, key_line)
            if key.value in members:
                raise ParseProblem("duplicate key", path, key_line)
            child = (*path, key.value)
            lines[child] = key_line
            members[key.value] = _yaml_value(value, child, depth + 1, lines, loader)
        return members
    if isinstance(node, yaml.SequenceNode):
        return [
            _yaml_value(item, (*path, index), depth + 1, lines, loader)
            for index, item in enumerate(node.value)
        ]
    assert isinstance(node, yaml.ScalarNode)
    if node.tag == "tag:yaml.org,2002:float":
        return _yaml_decimal(node.value, path, line)
    if node.tag == "tag:yaml.org,2002:int":
        return _yaml_integer(node, path, line, loader)
    try:
        return loader.construct_object(node)  # type: ignore[no-untyped-call]
    except (ValueError, OverflowError):
        raise ParseProblem("a value that cannot be read", path, line) from None


def _yaml_decimal(written: str, path: Path, line: int) -> Decimal:
    cleaned = written.replace("_", "")
    if not _YAML_DECIMAL.fullmatch(written) or cleaned in {"", "+", "-", ".", "+.", "-."}:
        # .inf, .nan and the base-60 form 1:30.5.
        raise ParseProblem("a number that is not finite or not decimal", path, line)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ParseProblem("a number that cannot be read", path, line) from None


def _yaml_integer(
    node: yaml.ScalarNode, path: Path, line: int, loader: yaml.SafeLoader
) -> int | Decimal:
    if _YAML_INTEGER.fullmatch(node.value) and len(node.value) > _INT_DIGITS:
        return Decimal(node.value.replace("_", ""))  # exact beyond Python's int limit
    try:
        value: int = loader.construct_object(node)  # type: ignore[no-untyped-call]
    except (ValueError, OverflowError):
        raise ParseProblem("a number that cannot be read", path, line) from None
    return value
