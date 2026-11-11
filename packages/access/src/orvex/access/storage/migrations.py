"""Bringing an access store to the code's schema version: forward only, in one transaction
(application).

Every pending migration and the version row it sets are written in one transaction, so a
failure anywhere leaves the store at the version it had. A store at a version above the code's
is refused and never migrated down: older code cannot know what the newer schema means.
"""

from __future__ import annotations

from orvex.access.errors import ACCESS_STORE_NEWER, AccessError
from orvex.access.storage.store import Store


def migrate(store: Store) -> int:
    """Apply the store's pending migrations; return the version it is at."""
    latest = len(store.migrations)
    with store.migration_transaction() as transaction:
        current = store.stored_version(transaction)
        if current > latest:
            raise AccessError(
                ACCESS_STORE_NEWER,
                "The access store was written by a newer version; upgrade before using it.",
                {"store_version": str(current), "code_version": str(latest)},
            )
        for version in range(current + 1, latest + 1):
            for statement in store.migrations[version - 1]:
                transaction.run(statement)
            transaction.run("UPDATE store_meta SET schema_version = :version", version=version)
    return latest
