"""The canonical bytes of an audit event: RFC 8785 (JCS) for the values an event may hold
(domain).

An event holds only strings, integers within +/-(2^53-1), booleans, `null`, arrays, and objects
whose member names are ASCII (`audit-event` v1). For those values JCS is exactly `json.dumps`
with sorted keys, no spaces and no ASCII escaping:
- member names are ASCII, so sorting by code point is sorting by UTF-16 code unit (JCS §3.2.3);
- `json.dumps` escapes the same characters as ECMAScript's `JSON.stringify`, with the same
  short forms and lower-case `\\u00xx` (§3.2.2.2);
- an integer in range prints the same digits in both (§3.2.2.3).

Anything outside that profile is refused, never approximated: a float, a large integer, a non-
ASCII member name, a lone surrogate. The tests check the equivalence against the vendored RFC 8785
test data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

MAX_SAFE_INTEGER = 2**53 - 1


class NotInProfile(ValueError):
    """A value an audit event may not hold."""


def _check(value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise NotInProfile("an integer beyond +/-(2^53-1)")
        return
    if isinstance(value, str):
        if any("\ud800" <= char <= "\udfff" for char in value):
            raise NotInProfile("a lone surrogate")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise NotInProfile("a member name that is not ASCII text")
            _check(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _check(item)
        return
    raise NotInProfile(f"a value of type {type(value).__name__}")


def canonical(document: Mapping[str, object]) -> bytes:
    """The JCS bytes of `document`, which must be within the profile above."""
    _check(document)
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")
