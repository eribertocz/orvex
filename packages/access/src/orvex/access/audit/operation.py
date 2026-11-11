"""One audited operation (application): its work and its event in one transaction, run again on
a transient conflict; a refusal recorded on its own; the log's own failure never retried
through the log (#437)."""

from __future__ import annotations

from collections.abc import Callable

from orvex.access.audit.log import AuditLog
from orvex.access.errors import AccessError
from orvex.access.storage import retry
from orvex.access.storage.store import Store, Transaction


def audited[T](
    store: Store,
    audit: AuditLog,
    operation: str,
    actor: str,
    work: Callable[[Transaction], T],
    *,
    project: str | None = None,
    target: str | None = None,
) -> T:
    """Run `work` (which records its own event through `audit.record`) and commit; on a
    refusal, record `access.refused` and raise it again."""
    try:
        done = retry.write(store, work)
    except AccessError as error:
        if error.code.startswith("AUDIT_"):
            audit.unavailable()  # the log itself failed: no second attempt through it
        else:
            audit.refusal(operation, actor, error.code, project=project, target=target)
        raise
    audit.committed()
    return done
