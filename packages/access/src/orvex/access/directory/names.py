"""The shapes the directory accepts for ids, user names, display names, groups and roles (domain).

- **Ids** (users, projects) are `security-context` v1 ids: opaque, never an email or a display
  name.
- **User names** are ASCII letters, digits and `. _ @ + -`, starting with a letter or a digit,
  at most 128 characters. They are unique without regard to case. ASCII alone makes `lower()`
  the same comparison in SQLite and PostgreSQL. It also keeps out look-alike letters from
  other scripts, which would let one user pass for another.
- **Display names** are free text for people, 1 to 200 characters, with no control character
  and no leading or trailing space.
- **Group names** are `security-context` v1 groups, in Unicode NFC form: the form policies and
  pages name them in (`security.md` §3.3). A name not already in NFC is refused, never repaired.
"""

from __future__ import annotations

import re
import unicodedata

from orvex.access.directory.records import ROLES, Role
from orvex.access.errors import VALIDATION_FAILED, AccessError

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_USER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _refuse(field: str) -> AccessError:
    return AccessError(VALIDATION_FAILED, f"The {field} is not valid.", {"field": field})


def checked_id(value: str, field: str) -> str:
    if not _ID.fullmatch(value):
        raise _refuse(field)
    return value


def checked_user_name(value: str) -> str:
    if not _USER_NAME.fullmatch(value):
        raise _refuse("user_name")
    return value


def checked_display_name(value: str) -> str:
    if not 1 <= len(value) <= 200 or _CONTROL.search(value) or value != value.strip():
        raise _refuse("display_name")
    return value


def checked_group(value: str) -> str:
    if (
        not 1 <= len(value) <= 64
        or _CONTROL.search(value)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise _refuse("group")
    return value


def checked_role(value: str) -> Role:
    for role in ROLES:
        if value == role:
            return role
    raise _refuse("role")
