"""The directory's locking reads (application): the rows a rule counts, held until the
transaction ends.

On PostgreSQL each read takes row locks (`FOR UPDATE`); on SQLite the transaction's
`BEGIN IMMEDIATE` already holds the database's only write lock and the reads lock nothing more.
Every operation takes its locks in the same order, so two operations never wait on each other in
a cycle: first the users (one read, in id order), then each project's Builder memberships (in
project order). The first-administrator command locks only the store's one metadata row.
"""

from __future__ import annotations

from orvex.access.directory.store import Transaction


def lock_users(transaction: Transaction, target: str, *, administrators: bool = False) -> None:
    """The target user's row and, when an administrator count decides, every administrator's,
    in id order and in one read, so two of these reads never wait on each other in a cycle."""
    transaction.rows(
        "SELECT id FROM users WHERE id = :target OR (administrator = :yes AND :administrators) "
        f"ORDER BY id{transaction.locking}",
        target=target,
        yes=True,
        administrators=administrators,
    )


def lock_builders(transaction: Transaction, project: str) -> None:
    """The project's Builder memberships: every change that can lower its count of active
    Builders (a removal, a demotion, a deactivation) takes this lock first."""
    transaction.rows(
        "SELECT user_id FROM memberships WHERE project = :project AND role = 'builder' "
        f"ORDER BY user_id{transaction.locking}",
        project=project,
    )


def lock_meta(transaction: Transaction) -> None:
    """The store's one metadata row: serializes the first-administrator command, which may find
    no administrator row to lock."""
    transaction.rows(f"SELECT id FROM store_meta{transaction.locking}")
