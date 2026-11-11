"""What the directory reports for the audit log: every refusal, and every use of the first-
administrator command (application).

The audit log itself is #437, which replaces the hook the directory is given; until then the
hook is the only record. An event names ids and codes, never a user name, a display name or a
recovery secret.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class DirectoryEvent:
    operation: str
    outcome: Literal["done", "refused"]
    actor: str | None  # None for the first-administrator command, which has no caller
    project: str | None = None
    target: str | None = None
    code: str | None = None  # the refusal's code


type AuditHook = Callable[[DirectoryEvent], None]
