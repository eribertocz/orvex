"""Passwords (domain): their rules, their Argon2id hashes and the random temporary ones (#438).

- **Rules** (#449 Q4): at least 12 characters, at most 1,024, no composition rules, and not the
  user name, compared without regard to case. Both sides are first normalized to NFKC, so a
  password typed on another keyboard is the same password.
- **Hashes:** Argon2id (RFC 9106), with at least OWASP's parameters, each stored in the hash. A
  stored hash below these parameters is replaced at the next successful sign-in. A stored value
  that is not an Argon2id hash never verifies.
- **No answer depends on whether a user exists:** `verify_nobody` runs the same hash against a
  password nobody has, so an unknown user costs what a wrong password costs.
"""

from __future__ import annotations

import contextlib
import secrets
import unicodedata

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from orvex.access.errors import PASSWORD_REJECTED, AccessError

MINIMUM_LENGTH = 12
MAXIMUM_LENGTH = 1024
# OWASP Password Storage cheat sheet: m = 19 MiB, t = 2, p = 1.
MEMORY_KIB = 19 * 1024
TIME_COST = 2
PARALLELISM = 1
HASH_BYTES = 32
SALT_BYTES = 16
TEMPORARY_BYTES = 18  # 24 characters, 144 random bits
_PREFIX = "$argon2id$"

_HASHER = PasswordHasher(
    time_cost=TIME_COST,
    memory_cost=MEMORY_KIB,
    parallelism=PARALLELISM,
    hash_len=HASH_BYTES,
    salt_len=SALT_BYTES,
    type=Type.ID,
)
# A hash of a password nobody can type: what an unknown user's sign-in is checked against.
_NOBODY = _HASHER.hash(secrets.token_hex(32))


def normalized(text: str) -> str:
    """NFKC: the form a password is checked, hashed and verified in."""
    return unicodedata.normalize("NFKC", text)


def checked(password: str, user_name: str) -> str:
    """The password, normalized, when it meets the rules; `PASSWORD_REJECTED` otherwise. The
    error never repeats the password."""
    normal = normalized(password)
    if not MINIMUM_LENGTH <= len(normal) <= MAXIMUM_LENGTH:
        raise AccessError(
            PASSWORD_REJECTED,
            f"A password has {MINIMUM_LENGTH} to {MAXIMUM_LENGTH} characters.",
            {"rule": "length"},
        )
    if normal.casefold() == normalized(user_name).casefold():
        raise AccessError(
            PASSWORD_REJECTED, "A password may not be the user name.", {"rule": "user_name"}
        )
    return normal


def hashed(password: str) -> str:
    """The Argon2id hash of a password that already passed `checked`."""
    return _HASHER.hash(password)


def verifies(stored: str, password: str) -> bool:
    """Whether `password` is the one `stored` hashes. Anything but an Argon2id hash is refused
    after the same work as a wrong password."""
    candidate = normalized(password)
    if not stored.startswith(_PREFIX) or len(candidate) > MAXIMUM_LENGTH:
        verify_nobody(password)
        return False
    try:
        return _HASHER.verify(stored, candidate)
    except VerifyMismatchError:
        return False
    except InvalidHashError, VerificationError:
        verify_nobody(password)
        return False


def verify_nobody(password: str) -> None:
    """The work of one verification, for a sign-in that has no hash to check."""
    with contextlib.suppress(VerificationError):
        _HASHER.verify(_NOBODY, normalized(password)[:MAXIMUM_LENGTH])


def needs_rehash(stored: str) -> bool:
    """Whether a hash that verified is below the parameters above, and so must be replaced."""
    try:
        found = extract_parameters(stored)
    except InvalidHashError:
        return True
    return (
        found.type is not Type.ID
        or found.memory_cost < MEMORY_KIB
        or found.time_cost < TIME_COST
        or found.hash_len < HASH_BYTES
        or found.salt_len < SALT_BYTES
    )


def temporary() -> str:
    """A random temporary password, shown once to the administrator who set it."""
    return secrets.token_urlsafe(TEMPORARY_BYTES)
