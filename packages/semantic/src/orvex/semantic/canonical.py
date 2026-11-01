"""Canonical JSON (RFC 8785, JSON Canonicalization Scheme) and fingerprints (P2-04a, #287).

Every fingerprint in the engine is the SHA-256 of canonical bytes made here: the model and its
members (P2-04e), queries (P2-05) and receipts (P2-07). The implementation uses the standard
library only:

- object members sorted by their UTF-16 code units (RFC 8785 §3.2.3);
- strings escaped as ECMAScript `JSON.stringify` does, and emitted as UTF-8 (§3.2.2.2);
- numbers serialized as ECMAScript `Number.prototype.toString` does (§3.2.2.3);
- I-JSON only (RFC 7493): a lone surrogate, a duplicate key, `NaN` and `Infinity` are refused.

A fingerprinted document holds integers within +/-(2^53-1) only; any other number is written as
a decimal string by its producer, so no exact value passes through binary floating point.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

MAX_SAFE_INTEGER = 2**53 - 1
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class NotCanonical(ValueError):
    """A value or text that has no canonical form: a programming error, never a user's."""


def number(value: float | int) -> str:
    """`value` as ECMAScript's `Number.prototype.toString` writes it (ECMA-262 §6.1.6.1.20)."""
    if isinstance(value, bool):
        raise NotCanonical("a boolean is not a number")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise NotCanonical("an integer beyond +/-(2^53-1); write it as a decimal string")
        return str(value)
    if not math.isfinite(value):
        raise NotCanonical("NaN and Infinity have no JSON form")
    if value == 0:
        return "0"  # negative zero included
    sign = "-" if value < 0 else ""
    # repr gives the shortest digits that round-trip, which is what ECMAScript requires.
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    trimmed = digits.rstrip("0")
    scale = int(exponent or "0") - len(fraction) + (len(digits) - len(trimmed))
    digits = trimmed
    k = len(digits)
    n = scale + k  # value = 0.digits * 10^n
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        power = n - 1
        text = digits[0] + ("." + digits[1:] if k > 1 else "")
        text += "e" + ("+" if power >= 0 else "-") + str(abs(power))
    return sign + text


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        if "\ud800" <= char <= "\udfff":
            raise NotCanonical("a lone surrogate is not I-JSON")
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif char < " ":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _sort_key(key: str) -> bytes:
    # Big-endian bytes compare as UTF-16 code units; a lone surrogate is refused when written.
    return key.encode("utf-16-be", "surrogatepass")


def _write(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int | float):
        out.append(number(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise NotCanonical("an object key is a string")
        out.append("{")
        for index, key in enumerate(sorted(value, key=_sort_key)):
            if index:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise NotCanonical(f"a {type(value).__name__} has no JSON form")


def canonicalize(value: object) -> bytes:
    """The RFC 8785 bytes of plain values (mappings, sequences, strings, numbers, booleans,
    None)."""
    out: list[str] = []
    _write(value, out)
    return "".join(out).encode("utf-8")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    members: dict[str, object] = {}
    for key, value in pairs:
        if key in members:
            raise NotCanonical("a duplicate key is not I-JSON")
        members[key] = value
    return members


def _no_constant(name: str) -> object:
    raise NotCanonical(f"{name} has no JSON form")


def canonicalize_text(text: str) -> bytes:
    """The RFC 8785 bytes of JSON text. Its numbers are IEEE-754 doubles, as RFC 8785 reads
    them; a duplicate key, a lone surrogate, `NaN` and `Infinity` are refused."""
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique,
            parse_int=float,
            parse_constant=_no_constant,
        )
    except json.JSONDecodeError as error:
        raise NotCanonical("not JSON text") from error
    return canonicalize(value)


def fingerprint(kind: str, format: int, document: object) -> str:
    """`sha256:` and the lowercase hex digest of the canonical bytes of `document` tagged with
    its `kind` and the `format` version of its layout."""
    tagged = {"format": format, "kind": kind, "value": document}
    return "sha256:" + hashlib.sha256(canonicalize(tagged)).hexdigest()
