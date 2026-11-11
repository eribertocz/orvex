"""Running a write transaction again when the database gave up on it (application).

PostgreSQL ends a `SERIALIZABLE` transaction that could break serializability (`40001`) or a
deadlock (`40P01`), and SQLite reports a busy database. Each is a `TransientConflict`: the whole
transaction runs again from the start, a few times with a short random pause, then the conflict
is raised. Nothing of a failed attempt was committed.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

from orvex.access.storage.store import Store, Transaction, TransientConflict

ATTEMPTS = 5


def write[T](store: Store, work: Callable[[Transaction], T]) -> T:
    attempt = 1
    while True:
        try:
            with store.transaction() as transaction:
                return work(transaction)
        except TransientConflict:
            if attempt == ATTEMPTS:
                raise
            time.sleep(0.01 * attempt + secrets.randbelow(10) / 1000)
            attempt += 1
