"""The instance's per-role query limits (#445; #449 Q8): for each project role, how many
questions one user may have running at once in one project, and how long one may run. Never a
limit on rows read or returned (Owner principle).

- Defaults: Builder 4 at once and 120 s, Author 2 and 60 s, Viewer 2 and 30 s (Q2 = A).
- Bounds: 1 to 64 at once; 1 s to 1 h.
- Only an active instance administrator changes them, and every change is audited
  (`instance.settings_changed`, naming the settings, never their values).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orvex.access.audit.log import AuditLog
from orvex.access.audit.operation import audited
from orvex.access.directory import rules
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.rules import refused
from orvex.access.errors import VALIDATION_FAILED, AccessError
from orvex.access.storage.store import Store, Transaction

ROLES = ("builder", "author", "viewer")
CONCURRENT = (1, 64)
RUN_MS = (1_000, 3_600_000)


@dataclass(frozen=True, slots=True)
class RoleLimit:
    max_concurrent: int
    max_run_ms: int


# The defaults the store starts with (Q2 = A), and local mode's one Builder's limits.
DEFAULT_LIMITS = {
    "builder": RoleLimit(4, 120_000),
    "author": RoleLimit(2, 60_000),
    "viewer": RoleLimit(2, 30_000),
}


def read(transaction: Transaction) -> dict[str, RoleLimit]:
    rows = transaction.rows("SELECT role, max_concurrent, max_run_ms FROM query_limits")
    return {str(r): RoleLimit(int(str(c)), int(str(m))) for r, c, m in rows}


def _checked(role: str, limit: RoleLimit) -> RoleLimit:
    if role not in ROLES:
        raise AccessError(VALIDATION_FAILED, "The role is not valid.", {"field": "role"})
    low, high = CONCURRENT
    if not low <= limit.max_concurrent <= high:
        raise AccessError(
            VALIDATION_FAILED, "The concurrency limit is not valid.", {"field": "max_concurrent"}
        )
    low, high = RUN_MS
    if not low <= limit.max_run_ms <= high:
        raise AccessError(
            VALIDATION_FAILED, "The run time limit is not valid.", {"field": "max_run_ms"}
        )
    return limit


class QueryLimits:
    """Read the per-role limits; change them as an instance administrator."""

    def __init__(self, store: Store, audit: AuditLog) -> None:
        self._store = store
        self._audit = audit

    def current(self) -> dict[str, RoleLimit]:
        with self._store.transaction(read_only=True) as transaction:
            return read(transaction)

    def change(self, actor: str, limits: Mapping[str, RoleLimit]) -> dict[str, RoleLimit]:
        """Change the roles given; the others keep theirs."""
        checked = {role: _checked(role, limit) for role, limit in limits.items()}

        def work(transaction: Transaction) -> dict[str, RoleLimit]:
            refused(rules.manage_users(directory_sql.caller(transaction, actor)))
            now = read(transaction)
            changed = sorted(r for r, limit in checked.items() if now.get(r) != limit)
            for role in changed:
                transaction.run(
                    "UPDATE query_limits SET max_concurrent = :concurrent, max_run_ms = :run "
                    "WHERE role = :role",
                    concurrent=checked[role].max_concurrent,
                    run=checked[role].max_run_ms,
                    role=role,
                )
            if changed:
                names = [f"query_limits.{role}" for role in changed]
                details = {"settings": names}
                self._audit.record(transaction, "instance.settings_changed", actor, details)
            return {**now, **{r: checked[r] for r in changed}}

        return audited(self._store, self._audit, "change_settings", actor, work)
