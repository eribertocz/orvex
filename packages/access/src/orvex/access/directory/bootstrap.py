"""The first administrator of a server instance, and its recovery (application): what
`<slug> admin create` does, with direct access to the instance and no HTTP route.

- **Initialization** migrates the store and, the first time only, writes the recovery file and
  records its digest. Once a digest is recorded, the file is never written again.
- **Create** makes an active administrator only while the instance has none.
- **Recover** (`--recover`) makes one even when administrators exist, but only with the recovery
  file whose digest the store holds, compared in constant time. A missing, wrong or unsafe file
  is refused like any other forbidden operation, saying nothing about which check failed.
- Every use is audited (#437): a creation as `user.created` in its own transaction, with the
  actor `system`; a refusal as `access.refused`. The two commands run serialized: each locks the
  store's metadata row first.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from orvex.access.audit.log import SYSTEM, AuditLog
from orvex.access.authentication import credentials, passwords
from orvex.access.directory import locks, recovery_file
from orvex.access.directory import statements as sql
from orvex.access.directory.names import checked_display_name, checked_user_name
from orvex.access.directory.records import NewUser
from orvex.access.directory.service import new_user_id
from orvex.access.errors import ACCESS_FORBIDDEN, USER_NAME_TAKEN, AccessError
from orvex.access.storage.instants import instant
from orvex.access.storage.migrations import migrate
from orvex.access.storage.store import Store, UniqueViolation


class Bootstrap:
    def __init__(
        self,
        store: Store,
        recovery: Path,
        audit: AuditLog,
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
    ) -> NewUser:
        """Create an active administrator with a temporary password (#438), returned once;
        `recover` with the recovery file at `recovery` (the instance's own by default)."""
        operation = "admin_recover" if recover else "admin_create"
        self.initialize()
        temporary = passwords.temporary()
        hashed_temporary = passwords.hashed(temporary)
        try:
            name = checked_user_name(user_name)
            display = checked_display_name(display_name)
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
                now = self._clock()
                try:
                    sql.insert_user(transaction, created, name, display, True, instant(now))
                except UniqueViolation as error:
                    raise AccessError(USER_NAME_TAKEN, "That user name is taken.") from error
                credentials.store(transaction, created, hashed_temporary, now, temporary=True)
                self._audit.record(
                    transaction, "user.created", SYSTEM, {"user": created, "via": operation}
                )
                found = sql.user(transaction, created)
        except AccessError as error:
            if error.code.startswith("AUDIT_"):
                self._audit.unavailable()  # the log itself failed: no second attempt through it
            else:
                self._audit.refusal(operation, SYSTEM, error.code)
            raise
        if found is None:
            raise AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")
        self._audit.committed()
        return NewUser(found, temporary)
