"""The product's `custom_extensions` payloads: the `ossie-extensions/v1` schema and the checks
that need the Ossie document around them.

The schema is a byte-identical copy of `contracts/ossie-extensions/v1/schema.json` (a test
compares their hashes). Its `pattern`s are applied as full matches: in JSON Schema a final `$`
also matches before a trailing newline, so every unescaped `$` outside a character class is read
as the end of the text (`\\Z`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import extend

SCHEMA_FILE = "ossie-extensions-v1.json"
TEMPORAL = frozenset({"Date", "Time", "DateTime", "DateTimeTz"})


def full_match(pattern: str) -> str:
    """`pattern` with every unescaped `$` outside a character class replaced by `\\Z`."""
    out: list[str] = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            out.append(pattern[index : index + 2])
            index += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "$":
            out.append(r"\Z")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(full_match(pattern))


def _pattern(
    validator: Validator, pattern: str, instance: object, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if isinstance(instance, str) and not _compiled(pattern).search(instance):
        yield ValidationError("does not match its pattern")


_FullMatchValidator = extend(Draft202012Validator, {"pattern": _pattern})  # type: ignore[no-untyped-call]


def full_match_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    """A Draft 2020-12 validator whose `pattern` matches the whole string: `$` never matches
    before a final newline, as Python's `re.search` would let it. Every contract schema in
    the engine is read with it."""
    _FullMatchValidator.check_schema(schema)
    result: Draft202012Validator = _FullMatchValidator(schema)
    return result


def schema_bytes() -> bytes:
    return files("orvex.semantic.ossie").joinpath(SCHEMA_FILE).read_bytes()


@cache
def validator() -> Validator:
    schema = json.loads(schema_bytes())
    _FullMatchValidator.check_schema(schema)
    result: Validator = _FullMatchValidator(schema)
    return result


def breaks_datatype_rules(payload: Mapping[str, object], datatype: object) -> bool:
    """Whether a field payload breaks a rule that depends on the field's declared Ossie
    `datatype` (#278, from P2-01's review): `time_zone` and `calendar` only on temporal types,
    `numeric` exactly on `Decimal` (`precision`, `scale`, scale at most precision) and `Integer`
    (`width`, `signed`). Without a `datatype` the type is unknown here (Ossie: "omit when
    unknown"); P2-04 types the field's expression and checks it then."""
    if datatype is None:
        return False
    if ("time_zone" in payload or "calendar" in payload) and datatype not in TEMPORAL:
        return True
    numeric = payload.get("numeric")
    if datatype not in ("Decimal", "Integer"):
        return numeric is not None
    if not isinstance(numeric, Mapping):
        return True
    wanted = {"precision", "scale"} if datatype == "Decimal" else {"width", "signed"}
    if set(numeric) != wanted:
        return True
    return datatype == "Decimal" and int(str(numeric["scale"])) > int(str(numeric["precision"]))
