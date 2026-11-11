"""The access module's refusals: one exception, each with a stable code.

The codes a caller of the HTTP API can receive are registered in `http-api` v1
(`components.x-error-codes`); the administration routes that send them come in #446. The store's
own refusals (`ACCESS_STORE_*`) and the audit log's (`AUDIT_*`, except `AUDIT_UNAVAILABLE`, which
refuses the request it would have recorded) stop the server or a command, or report on the log.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# The caller may not perform this operation; the message never says which rule refused it.
ACCESS_FORBIDDEN = "ACCESS_FORBIDDEN"
# The change would leave a project without an active Builder.
LAST_BUILDER = "LAST_BUILDER"
# The change would leave the instance without an active administrator.
LAST_ADMINISTRATOR = "LAST_ADMINISTRATOR"
# Another user already has this user name, compared without regard to case.
USER_NAME_TAKEN = "USER_NAME_TAKEN"
# A name, id or role outside the shapes the directory accepts.
VALIDATION_FAILED = "VALIDATION_FAILED"
# The store was written by a newer version of the code; it is never migrated down.
ACCESS_STORE_NEWER = "ACCESS_STORE_NEWER"
# The store cannot be proven to be the access module's own: refused before it is used.
ACCESS_STORE_UNSAFE = "ACCESS_STORE_UNSAFE"
# The audit log (#437). An event that breaks `audit-event` v1 is never written.
AUDIT_EVENT_INVALID = "AUDIT_EVENT_INVALID"
# An action whose audit event could not be recorded did not happen.
AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
# Retention below its minimum, or with old events but no signed checkpoint to stop at.
AUDIT_RETENTION_INVALID = "AUDIT_RETENTION_INVALID"
# `verify`: an event that does not follow from the one before it, named by its id.
AUDIT_CHAIN_BROKEN = "AUDIT_CHAIN_BROKEN"
# `verify`: a checkpoint, the head or an anchor whose signature or value does not hold.
AUDIT_CHECKPOINT_INVALID = "AUDIT_CHECKPOINT_INVALID"
# The signing key's file is missing, unsafe, or not the key the log knows.
AUDIT_KEY_UNSAFE = "AUDIT_KEY_UNSAFE"


class AccessError(Exception):
    """A refusal with a stable `code`, a sentence for people, and details safe to show."""

    def __init__(self, code: str, message: str, details: Mapping[str, str] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: Mapping[str, str] = MappingProxyType(dict(details or {}))
