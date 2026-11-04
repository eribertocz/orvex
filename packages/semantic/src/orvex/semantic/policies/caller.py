"""The caller a `security-context` v1 document describes, as the semantic engine reads it.

The document must be the one built for this request (`current_fingerprint`, P2-09). A malformed
one, or one whose groups are not normalized, is refused: it is never read as a caller with
fewer or more permissions. The catalog (P2-10) and the correctness engine (P2-11) read the
caller only through here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class InvalidContext(ValueError):
    """Not a well-formed `security-context` v1 document: an internal error, never a user's."""


@dataclass(frozen=True, slots=True)
class Caller:
    groups: tuple[str, ...]  # sorted, without duplicates
    builder: bool


def read_caller(context: Mapping[str, Any]) -> Caller:
    from orvex.contracts.security_context.v1 import SecurityContext

    try:
        parsed = SecurityContext.model_validate(dict(context))
    except ValueError as error:
        raise InvalidContext("not a security-context v1 document") from error
    groups = [str(getattr(group, "root", group)) for group in parsed.groups]
    if groups != sorted(set(groups)):
        raise InvalidContext("the context's groups are not normalized")
    return Caller(tuple(groups), parsed.role == "builder")
