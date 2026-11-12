"""Attribute names and values (domain, #440): exactly the `security-context` v1 types, checked
where they are written and never coerced.

- **Names** match `^[a-z][a-z0-9_]{0,62}$`, and are not one of the built-in context variables
  (`user_id`, `user`, `project`, `role`, `groups`).
- **`text`:** 1 to 1,024 characters, already in NFC.
- **`integer`:** the contract's decimal text `^(0|-?[1-9][0-9]{0,37})$` (so never `-0`), within
  the signed 64-bit range.
- **`boolean`:** JSON `true` or `false` only.
- **`list`:** 1 to 1,000 texts, each a valid `text`, no two equal. Stored sorted by code point.
- **Nothing is converted.** `"1"` is not a boolean, `1.0` and `"01"` are not integers, and text
  that is not NFC is refused rather than normalized. A value is never trimmed, case-folded or
  truncated. Every refusal is `VALIDATION_FAILED`, naming the field and never echoing the value.
- `typed` reads a stored attribute back through the same checks: a stored value that fails them
  is refused, never repaired.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Literal

from orvex.access.errors import VALIDATION_FAILED, AccessError

type Kind = Literal["text", "integer", "boolean", "list"]

KINDS: tuple[Kind, ...] = ("text", "integer", "boolean", "list")
RESERVED = frozenset({"user_id", "user", "project", "role", "groups"})
MAXIMUM_PER_USER = 64
_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_INTEGER = re.compile(r"0|-?[1-9][0-9]{0,37}")  # the contract's own pattern: no "-0"
_LONGEST_TEXT = 1024
_LONGEST_LIST = 1000
_INT64 = (-(2**63), 2**63 - 1)


def invalid(field: str) -> AccessError:
    return AccessError(VALIDATION_FAILED, f"The {field} is not valid.", {"field": field})


def checked_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in RESERVED:
        raise invalid("name")
    return name


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _LONGEST_TEXT
        or unicodedata.normalize("NFC", value) != value
    ):
        raise invalid("value")
    return value


def checked(kind: object, value: object) -> dict[str, Any]:
    """The attribute in its `security-context` v1 form, or `VALIDATION_FAILED`."""
    if kind == "text":
        return {"type": "text", "value": _text(value)}
    if kind == "integer":
        if not isinstance(value, str) or not _INTEGER.fullmatch(value):
            raise invalid("value")
        low, high = _INT64
        if not low <= int(value) <= high:
            raise invalid("value")
        return {"type": "integer", "value": value}
    if kind == "boolean":
        if value is not True and value is not False:
            raise invalid("value")
        return {"type": "boolean", "value": value}
    if kind == "list":
        if not isinstance(value, list) or not 1 <= len(value) <= _LONGEST_LIST:
            raise invalid("value")
        items = [_text(item) for item in value]
        if len(set(items)) != len(items):
            raise invalid("value")
        return {"type": "list", "values": sorted(items)}
    raise invalid("type")


def stored(attribute: dict[str, Any]) -> tuple[str, str]:
    """(type, value text) for the store: the value as canonical JSON."""
    kind = str(attribute["type"])
    value = attribute["values"] if kind == "list" else attribute["value"]
    return kind, json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def typed(kind: str, text: str) -> dict[str, Any]:
    """A stored attribute, read back through every check; `VALIDATION_FAILED` if it fails."""
    try:
        value = json.loads(text)
    except ValueError as error:
        raise invalid("value") from error
    found = checked(kind, value)
    if stored(found)[1] != text:  # not in its one canonical form: refused, never repaired
        raise invalid("value")
    return found
