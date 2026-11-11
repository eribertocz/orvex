"""Where an instance keeps its audit signing key and anchor file, and how long it keeps events
(#437): the settings the `admin` commands and the server-mode start share.

| Setting | Variable | Default |
|---|---|---|
| signing key | `<SLUG>_AUDIT_SIGNING_KEY` | `audit-signing.key` in the instance directory |
| anchor file | `<SLUG>_AUDIT_ANCHORS` | `audit-anchors.log` in the instance directory |
| retention | `<SLUG>_AUDIT_RETENTION_DAYS` | 730, at least 30 |

In server mode the key's path must be out of the PostgreSQL server's reach, and the anchor file
is meant to be shipped elsewhere (`audit-event` v1 README). A retention that is not a whole
number of at least 30 days is refused, never replaced by the default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orvex.access.audit.log import MINIMUM_RETENTION_DAYS
from orvex.access.errors import AUDIT_RETENTION_INVALID, AccessError
from orvex.server.identity import Identity

KEY_FILE = "audit-signing.key"
ANCHORS_FILE = "audit-anchors.log"
DEFAULT_RETENTION_DAYS = 730


@dataclass(frozen=True, slots=True)
class AuditSettings:
    key: Path
    anchors: Path
    retention_days: int


def audit_settings(
    identity: Identity, instance: Path, environment: Mapping[str, str]
) -> AuditSettings:
    prefix = identity.slug.upper().replace("-", "_")
    key = environment.get(f"{prefix}_AUDIT_SIGNING_KEY") or str(instance / KEY_FILE)
    anchors = environment.get(f"{prefix}_AUDIT_ANCHORS") or str(instance / ANCHORS_FILE)
    days_text = environment.get(f"{prefix}_AUDIT_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    if not days_text.isdecimal() or int(days_text) < MINIMUM_RETENTION_DAYS:
        raise AccessError(
            AUDIT_RETENTION_INVALID,
            f"Audit retention is a whole number of at least {MINIMUM_RETENTION_DAYS} days.",
            {"retention_days": days_text[:32]},
        )
    return AuditSettings(Path(key), Path(anchors), int(days_text))
