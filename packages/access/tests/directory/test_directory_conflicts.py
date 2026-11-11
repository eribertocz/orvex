"""#436: what the directory does when the database reports a conflict: a transaction the
database gave up on is run again a few times and then fails; a uniqueness race is the
operation's own refusal, never a driver error."""

from __future__ import annotations

from contextlib import AbstractContextManager

import pytest
from orvex.access.directory import Directory
from orvex.access.errors import USER_NAME_TAKEN, AccessError
from orvex.access.storage.retry import ATTEMPTS
from orvex.access.storage.store import Transaction, TransientConflict, UniqueViolation

from tests.support.access_directory import AnyStore, World


class _GivenUp(AbstractContextManager[Transaction]):
    """A transaction the database ends at once, as PostgreSQL does on a serialization failure."""

    def __enter__(self) -> Transaction:
        raise TransientConflict("40001")

    def __exit__(self, *details: object) -> None:
        return None


class Flaky:
    """A store that gives up on the first `failures` transactions, then is `inner`."""

    def __init__(self, inner: AnyStore, failures: int) -> None:
        self._inner = inner
        self.failures = failures
        self.migrations = inner.migrations

    def transaction(self, *, read_only: bool = False) -> AbstractContextManager[Transaction]:
        if not read_only and self.failures:
            self.failures -= 1
            return _GivenUp()
        return self._inner.transaction(read_only=read_only)

    def migration_transaction(self) -> AbstractContextManager[Transaction]:
        return self._inner.migration_transaction()

    def stored_version(self, transaction: Transaction) -> int:
        return self._inner.stored_version(transaction)

    def remove_audit_through(self, transaction: Transaction, seq: int) -> None:
        self._inner.remove_audit_through(transaction, seq)


def test_a_transaction_the_database_gave_up_on_runs_again(world: World) -> None:
    flaky = Flaky(world.store, failures=ATTEMPTS - 1)
    created = Directory(flaky, world.audit).create_user(world.admin, "ana", "Ana")
    assert world.directory.user(created.id) == created
    assert flaky.failures == 0


def test_it_fails_after_the_last_attempt_and_changes_nothing(world: World) -> None:
    flaky = Flaky(world.store, failures=ATTEMPTS)
    with pytest.raises(TransientConflict):
        Directory(flaky, world.audit).create_user(world.admin, "ana", "Ana")
    assert world.refusals() == []  # not a refusal: the caller may try again


def test_both_stores_report_a_uniqueness_conflict_as_such(world: World) -> None:
    insert = "INSERT INTO project_groups (project, name) VALUES ('p1', 'twice')"
    with world.store.transaction() as transaction:
        transaction.run(insert)
    with pytest.raises(UniqueViolation), world.store.transaction() as transaction:
        transaction.run(insert)


def test_a_user_name_lost_to_a_concurrent_writer_is_user_name_taken(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name was free when checked; another writer took it before the insert."""
    world.user("ana")
    monkeypatch.setattr(
        "orvex.access.directory.statements.user_by_name", lambda transaction, name: None
    )
    with pytest.raises(AccessError) as refused:
        world.directory.create_user(world.admin, "ANA", "Other")
    assert refused.value.code == USER_NAME_TAKEN
