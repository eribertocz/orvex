"""The instance's audit signing key: Ed25519, in a file only its owner can read, never in the
database (adapter).

- **Created** once, owner-only (`0600`), exclusively: an existing file is never overwritten.
- **Read** only if it is a regular file (not a link, opened with `O_NOFOLLOW`), owned by the
  user running the process, and readable by nobody else. Anything else is refused.
- A key is named by its fingerprint: `sha256:` and the SHA-256 of its raw public key.
- In server mode, the file must be out of the database server's reach. Its path is an instance
  setting (`server/audit_settings.py`).
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from orvex.access.errors import AUDIT_KEY_UNSAFE, AccessError

_MAX_BYTES = 4096


def fingerprint(public: bytes) -> str:
    return "sha256:" + hashlib.sha256(public).hexdigest()


def verifies(public: bytes, signature: bytes, payload: bytes) -> bool:
    """Whether `signature` is `public`'s Ed25519 signature of `payload`."""
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    except InvalidSignature, ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class SigningKey:
    _private: Ed25519PrivateKey

    @property
    def public(self) -> bytes:
        return self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public)

    def sign(self, payload: bytes) -> bytes:
        return self._private.sign(payload)


def _unsafe(reason: str) -> AccessError:
    return AccessError(AUDIT_KEY_UNSAFE, f"The audit signing key {reason}.", {})


def create(path: Path) -> SigningKey:
    """A new key, written to `path`, which must not exist."""
    key = Ed25519PrivateKey.generate()
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    return SigningKey(key)


def load(path: Path) -> SigningKey:
    """The key in `path`, refused unless the file is safe (above)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise _unsafe("file is missing") from error
    except OSError as error:
        raise _unsafe("file cannot be opened safely") from error
    with os.fdopen(descriptor, "rb") as file:
        status = os.fstat(file.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
            raise _unsafe("file is not an owner-only regular file")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise _unsafe("file belongs to another user")
        data = file.read(_MAX_BYTES)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as error:
        raise _unsafe("file does not hold a key") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise _unsafe("file does not hold an Ed25519 key")
    return SigningKey(key)
