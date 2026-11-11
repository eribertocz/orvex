"""The first administrator of a server instance, and its recovery (application): what
`<slug> admin create` does, with direct access to the instance and no HTTP route.

- **Initialization** migrates the store and, the first time only, writes the recovery file and
  records its digest. Once a digest is recorded, the file is never written again.
- **Create** makes an active administrator only while the instance has none.
- **Recover** (`--recover`) makes one even when administrators exist, but only with the recovery
  file whose digest the store holds, compared in constant time. A missing, wrong or unsafe file
  is refused like any other forbidden operation, saying nothing about which check failed.
- Every use, done or refused, goes to the audit hook. The two run serialized: each locks the
  store's metadata row first.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from orvex.access.directory import locks, recovery_file
from orvex.access.directory import statements as sql
from orvex.access.directory.events import AuditHook, DirectoryEvent
from orvex.access.directory.migrations import migrate
from orvex.access.directory.names import checked_display_name, checked_user_name
from orvex.access.directory.records import User
from orvex.access.directory.service import instant, new_user_id
from orvex.access.directory.store import Store, UniqueViolation
from orvex.access.errors import ACCESS_FORBIDDEN, USER_NAME_TAKEN, AccessError


class Bootstrap:
    def __init__(
        self,
        store: Store,
        recovery: Path,
        audit: AuditHook,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = new_user_id,
    ) -> None:
        self._store = store
        self._recovery = recovery
        self._audit = audit
        self._clock = clock
        self._new_id = new_id

    def initialize(self) -> bool:
        """Migrate the store; write the recovery file the first time only (then return True)."""
        migrate(self._store)
        with self._store.transaction() as transaction:
            locks.lock_meta(transaction)
            if sql.recovery_digest(transaction) is not None:
                return False
            # The file first: if the transaction then fails, no digest names it, and the next
            # initialization replaces it.
            sql.set_recovery_digest(transaction, recovery_file.write_new(self._recovery))
            return True

    def create_administrator(
        self,
        user_name: str,
        display_name: str,
        *,
        recover: bool = False,
        recovery: Path | None = None,
    ) -> User:
        """Create an active administrator; `recover` with the recovery file at `recovery` (the
        instance's own by default)."""
        operation = "admin_recover" if recover else "admin_create"
        try:
            name = checked_user_name(user_name)
            display = checked_display_name(display_name)
            self.initialize()
            with self._store.transaction() as transaction:
                locks.lock_meta(transaction)
                if recover:
                    given = recovery_file.read_digest(recovery or self._recovery)
                    stored = sql.recovery_digest(transaction)
                    if given is None or stored is None or not hmac.compare_digest(given, stored):
                        raise AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")
                elif sql.other_active_administrators(transaction, excluding="") > 0:
                    raise AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")
                if sql.user_by_name(transaction, name) is not None:
                    raise AccessError(USER_NAME_TAKEN, "That user name is taken.")
                created = self._new_id()
                try:
                    sql.insert_user(
                        transaction, created, name, display, True, instant(self._clock())
                    )
                except UniqueViolation as error:
                    raise AccessError(USER_NAME_TAKEN, "That user name is taken.") from error
                found = sql.user(transaction, created)
        except AccessError as error:
            self._audit(DirectoryEvent(operation, "refused", None, code=error.code))
            raise
        if found is None:
            raise AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")
        self._audit(DirectoryEvent(operation, "done", None, target=found.id))
        return found
