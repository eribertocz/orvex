"""The security context and the permission fingerprint (`security-context` v1, P2-09, #306).

The semantic engine receives the caller's identity and permissions in one typed, immutable,
validated form, and never reads users or sessions itself.

- **Normalization before validation** (Owner decision of 2026-11-02). The provider normalizes
  lists exactly once: groups and list attributes are NFC-normalized, deduplicated and sorted by
  code point. Nothing else is normalized: no case folding and no trimming. `read_context`
  accepts only the normalized form; it never repairs one.
- **The permission fingerprint** covers everything that can change what the caller may see:
  - the user, project, role, groups and attributes;
  - the policy model's fingerprint (P2-08);
  - the snapshot of every managed-lake table the policies read.
- **Current permissions before reuse.** A cached result or a cursor is reused only when the
  fingerprint computed anew for the request equals the stored one (`may_reuse`). A fingerprint
  carried by a request is never trusted on its own.
- **The instance-administrator flag never reaches a context:** administration grants no data
  access (Owner decision on #238).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.validators import extend

SCHEMA_FILE = "security-context-v1.json"
DOMAIN = b"security-context/v1\n"
FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
Role = Literal["builder", "author", "viewer"]


class InvalidContext(ValueError):
    """A context that breaks `security-context` v1: the caller gets no access."""


def _full_match(pattern: str) -> str:
    """`pattern` with every unescaped `$` outside a character class replaced by `\\Z`, so it
    never matches before a final newline as Python's `re.search` would let it."""
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
    return re.compile(_full_match(pattern))


def _pattern(
    validator: object, pattern: str, instance: object, schema: object
) -> Iterable[ValidationError]:
    if isinstance(instance, str) and not _compiled(pattern).search(instance):
        yield ValidationError("does not match its pattern")


_FullMatch = extend(Draft202012Validator, {"pattern": _pattern})  # type: ignore[no-untyped-call]


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.access").joinpath(SCHEMA_FILE).read_bytes())
    _FullMatch.check_schema(schema)
    validator: Draft202012Validator = _FullMatch(schema)
    return validator


def normalize_list(values: Iterable[str]) -> list[str]:
    """NFC, then without duplicates, sorted by code point: the only normalization there is.
    Anything but text is refused, never converted (`1` and `"1"` are not one group)."""
    found = list(values)
    if not all(isinstance(value, str) for value in found):
        raise InvalidContext("a group or a list attribute holds text only")
    return sorted({unicodedata.normalize("NFC", value) for value in found})


def _encode(value: object) -> bytes:
    """An unambiguous byte form: a type tag, a length, then the content (README)."""
    if isinstance(value, bool):
        return b"b" + (b"\x01" if value else b"\x00")
    if isinstance(value, str):
        data = value.encode("utf-8")
        return b"s" + len(data).to_bytes(8, "big") + data
    if isinstance(value, list | tuple):
        return b"l" + len(value).to_bytes(8, "big") + b"".join(_encode(item) for item in value)
    if isinstance(value, Mapping):
        keys = sorted(value, key=lambda key: str(key).encode("utf-8"))
        return (
            b"m"
            + len(keys).to_bytes(8, "big")
            + b"".join(_encode(str(k)) + _encode(value[k]) for k in keys)
        )
    raise InvalidContext(f"a {type(value).__name__} has no fingerprint form")


def permission_fingerprint(
    *,
    user: str,
    project: str,
    role: str,
    groups: Iterable[str],
    attributes: Mapping[str, Mapping[str, Any]],
    policy_fingerprint: str,
    mapping_snapshots: Mapping[str, str],
) -> str:
    """`sha256:` over the byte form of everything that can change what the caller may see."""
    if not FINGERPRINT.fullmatch(policy_fingerprint):
        raise InvalidContext("the policy fingerprint is sha256: and 64 lowercase hex digits")
    document = {
        "user": user,
        "project": project,
        "role": role,
        "groups": list(groups),
        "attributes": {name: dict(value) for name, value in attributes.items()},
        "policies": policy_fingerprint,
        "mapping_snapshots": dict(mapping_snapshots),
    }
    return "sha256:" + hashlib.sha256(DOMAIN + _encode(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class SecurityContext:
    """A validated, immutable context. Read it with `read_context`; produce it with a
    provider."""

    user: str
    project: str
    role: Role
    groups: tuple[str, ...]
    attributes: Mapping[str, Mapping[str, Any]] = field(repr=False)
    permission_fingerprint: str

    def document(self) -> dict[str, Any]:
        return {
            "kind": "security_context",
            "version": 1,
            "user": self.user,
            "project": self.project,
            "role": self.role,
            "groups": list(self.groups),
            "attributes": {name: _plain(value) for name, value in self.attributes.items()},
            "permission_fingerprint": self.permission_fingerprint,
        }


def _plain(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: list(item) if isinstance(item, tuple) else item for key, item in value.items()}


def _frozen(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({k: tuple(v) if isinstance(v, list) else v for k, v in value.items()})


def read_context(document: Mapping[str, Any]) -> SecurityContext:
    """A context from its contract document, or `InvalidContext`. A list that is not already
    normalized is refused, never repaired: producer and consumer agree on one form."""
    problems = list(_validator().iter_errors(document))
    if problems:
        raise InvalidContext(problems[0].message)
    groups = list(document["groups"])
    if groups != normalize_list(groups):
        raise InvalidContext("groups are not normalized (NFC, unique, sorted by code point)")
    attributes: dict[str, Mapping[str, Any]] = {}
    for name, value in document["attributes"].items():
        if value["type"] == "list" and list(value["values"]) != normalize_list(value["values"]):
            raise InvalidContext(f"the list attribute {name} is not normalized")
        attributes[name] = _frozen(value)
    return SecurityContext(
        user=document["user"],
        project=document["project"],
        role=document["role"],
        groups=tuple(groups),
        attributes=MappingProxyType(attributes),
        permission_fingerprint=document["permission_fingerprint"],
    )


class ContextProvider(Protocol):
    """Builds a caller's context anew, from current state, for each request."""

    def context_for(self, request: object) -> SecurityContext: ...


def current_fingerprint(provider: ContextProvider, request: object) -> str:
    """The permission fingerprint of the caller's current context, computed for this request."""
    return provider.context_for(request).permission_fingerprint


def may_reuse(stored_fingerprint: object, provider: ContextProvider, request: object) -> bool:
    """Whether a cached result or a cursor stored under `stored_fingerprint` may serve this
    request: only when the caller's current permissions give the same fingerprint."""
    if not isinstance(stored_fingerprint, str) or not FINGERPRINT.fullmatch(stored_fingerprint):
        return False
    try:
        fresh = current_fingerprint(provider, request)
    except InvalidContext:
        return False
    return hmac.compare_digest(fresh, stored_fingerprint)
