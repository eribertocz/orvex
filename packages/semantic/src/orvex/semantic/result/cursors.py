"""The page-cursor contract of `semantic-result` v1: how cursors are issued and verified (P2-06).

A cursor binds the query and model fingerprints, the caller's permission fingerprint, the data
version, the page size and the position (the sort-key values of the last row returned). Its
form is a `CursorForm`, chosen by the first execution issue (P2-12) under the dependency policy:

- **sealed**: authenticated encryption under a server-held key, with a fresh nonce per cursor
  and the bindings as authenticated data;
- **server-side**: a random handle of at least 128 bits pointing to state the server keeps.

Either way the cursor reveals nothing it holds, and two cursors for one position differ.
`verify_cursor` holds the refusal rules, the same for every form: anything but a genuine cursor
presented with the same bindings on an available snapshot is `PAGE_CURSOR_INVALID`, and the
reason is `expired` only when every other check passed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from orvex.semantic.errors import SemanticError

CURSOR = re.compile(r"[A-Za-z0-9_-]{1,1024}")
FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")

type Key = str | int | float | bool | None
type Position = tuple[Key, ...]


@dataclass(frozen=True, slots=True)
class CursorBinding:
    """What a page request is bound to, computed anew for every request with the caller's
    current security context (a cursor never grants access)."""

    query_fingerprint: str
    model_fingerprint: str
    permission_fingerprint: str
    page_size: int

    def __post_init__(self) -> None:
        for name in ("query_fingerprint", "model_fingerprint", "permission_fingerprint"):
            if not FINGERPRINT.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} is sha256: and 64 lowercase hex digits")
        if isinstance(self.page_size, bool) or not isinstance(self.page_size, int):
            raise ValueError("page_size is an integer")
        if self.page_size < 1:
            raise ValueError("page_size is positive")


@dataclass(frozen=True, slots=True)
class CursorState:
    """What a genuine cursor holds besides its bindings: the data version (the snapshot on
    DuckLake routes) every page reads, and the keyset position."""

    data_version: str
    position: Position


class SnapshotStatus(StrEnum):
    """How the route sees a cursor's data version."""

    AVAILABLE = "available"
    EXPIRED = "expired"
    """A snapshot this route served that is no longer kept."""
    FOREIGN = "foreign"
    """A data version this route never serves: the cursor is presented with another one."""


class CursorForm(Protocol):
    """A cursor form: sealed or server-side."""

    def issue(self, binding: CursorBinding, state: CursorState) -> str:
        """A new cursor for `state` under `binding`, never equal to an earlier one."""
        ...

    def open(self, cursor: str, binding: CursorBinding) -> CursorState | None:
        """The state of a genuine cursor issued under exactly `binding`, else None: forged,
        altered, unknown or issued under any other binding."""
        ...


def refused(reason: str) -> SemanticError:
    return SemanticError("PAGE_CURSOR_INVALID", {"reason": reason})


def issue_cursor(form: CursorForm, binding: CursorBinding, state: CursorState) -> str:
    """A cursor in the contract's format (1 to 1,024 URL-safe base64 characters)."""
    cursor = form.issue(binding, state)
    if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
        raise ValueError("a cursor is 1-1,024 characters of the URL-safe base64 alphabet")
    return cursor


def verify_cursor(
    form: CursorForm,
    cursor: object,
    binding: CursorBinding,
    snapshot: Callable[[str], SnapshotStatus],
) -> CursorState:
    """The state of `cursor` for this request, or `PAGE_CURSOR_INVALID`: `reason: invalid` for
    a malformed, forged, altered or unknown cursor, or one presented with another caller,
    query, model, page size, permissions or data version; `reason: expired` only for a genuine
    cursor with every binding equal whose snapshot is no longer available."""
    if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
        raise refused("invalid")
    state = form.open(cursor, binding)
    if state is None:
        raise refused("invalid")
    status = snapshot(state.data_version)
    if status is SnapshotStatus.AVAILABLE:
        return state
    raise refused("expired" if status is SnapshotStatus.EXPIRED else "invalid")


class SealedForm:
    """The sealed cursor form (P2-24, #363): AES-256-GCM under keys derived from the instance's
    cursor secret, in two layers, each with a fresh 96-bit nonce.

    - The **inner** layer holds the data version and the position, compressed then encrypted,
      with every binding as authenticated data: any change to it or to a binding makes it fail
      to open.
    - The **outer** layer encrypts the data version and the inner layer under a second key. It
      lets the server learn a genuine cursor's snapshot before it compiles the model there
      (`data_version`), without the bindings, which depend on that model. The inner layer
      repeats the version, and opening requires both to agree.

    Either way the cursor reveals nothing to a client, and two cursors for one position
    differ."""

    DOMAIN = b"orvex-semantic page cursor v1"
    OUTER = b"orvex-semantic page cursor version v1"

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("a cursor secret has 32 bytes or more")
        self._aead = AESGCM(hmac.new(secret, self.DOMAIN, hashlib.sha256).digest())
        self._outer = AESGCM(hmac.new(secret, self.OUTER, hashlib.sha256).digest())

    @staticmethod
    def _bound(binding: CursorBinding) -> bytes:
        return json.dumps(
            [
                binding.query_fingerprint,
                binding.model_fingerprint,
                binding.permission_fingerprint,
                binding.page_size,
            ],
            separators=(",", ":"),
        ).encode()

    def issue(self, binding: CursorBinding, state: CursorState) -> str:
        version = state.data_version.encode()
        if len(version) > 255:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "cursor_size"})
        plain = json.dumps(
            [state.data_version, list(state.position)], separators=(",", ":"), allow_nan=False
        ).encode()
        nonce = secrets.token_bytes(12)
        inner = nonce + self._aead.encrypt(nonce, zlib.compress(plain, 9), self._bound(binding))
        outer_nonce = secrets.token_bytes(12)
        outer = self._outer.encrypt(outer_nonce, bytes([len(version)]) + version + inner, None)
        cursor = base64.urlsafe_b64encode(outer_nonce + outer).rstrip(b"=").decode("ascii")
        if len(cursor) > 1024:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "cursor_size"})
        return cursor

    def _unwrap(self, cursor: str) -> tuple[str, bytes] | None:
        """The data version and the inner layer of a genuine cursor of this instance."""
        try:
            sealed = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            # Only the canonical text: Base64 ignores the last character's unused bits, so a
            # cursor changed there would decode to the same bytes (#385).
            if base64.urlsafe_b64encode(sealed).rstrip(b"=").decode("ascii") != cursor:
                return None
            plain = self._outer.decrypt(sealed[:12], sealed[12:], None)
            size = plain[0]
            version = plain[1 : 1 + size].decode()
        except ValueError, InvalidTag, IndexError, TypeError:
            return None
        if len(plain) < 1 + size:
            return None
        return version, plain[1 + size :]

    def data_version(self, cursor: object) -> str | None:
        """The data version a genuine cursor of this instance pins, or None. It authenticates
        the cursor but checks no binding: `verify_cursor` still does, on every page."""
        if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
            return None
        unwrapped = self._unwrap(cursor)
        return None if unwrapped is None else unwrapped[0]

    def open(self, cursor: str, binding: CursorBinding) -> CursorState | None:
        unwrapped = self._unwrap(cursor)
        if unwrapped is None:
            return None
        outer_version, inner = unwrapped
        try:
            plain = zlib.decompress(
                self._aead.decrypt(inner[:12], inner[12:], self._bound(binding))
            )
            version, position = json.loads(plain)
        except ValueError, InvalidTag, zlib.error, TypeError:
            return None
        if (
            not isinstance(version, str)
            or not isinstance(position, list)
            or version != outer_version
        ):
            return None
        if not all(v is None or isinstance(v, str | int | float | bool) for v in position):
            return None
        return CursorState(version, tuple(position))
