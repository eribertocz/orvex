"""Faults the server's tests inject at the store contract's binding: one operation's call wrapped,
so a test acts around it (a crash, an interrupt, another writer) without reaching into the
store's internals (#486 G2)."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

from orvex.contracts.binding import Bound
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.store.errors import StoreError
from orvex.contracts.store.v1 import StatementRequest
from orvex.contracts.store.values import LakeHandle

STORE = "store"


@dataclasses.dataclass(frozen=True, slots=True)
class Call:
    """One intercepted call: the operation sent on, and the store to send others to, past the
    interception (another writer's commit is not the intercepted one)."""

    bound: Bound
    envelope: CallEnvelope
    arguments: tuple[Any, ...]
    proceed: Callable[[], Any]
    real: Callable[..., Any]

    def send(self, operation: str, *arguments: object) -> Any:  # noqa: ANN401 - its result
        key = uuid.uuid4().hex if self.bound.contract.operation(operation).changes_state else None
        envelope = dataclasses.replace(self.envelope, idempotency_key=key)
        return self.real(self.bound, operation, envelope, *arguments)

    def another_writer(self, sql: str) -> None:
        """Another writer of the one open lake commits `sql` in a transaction of its own."""
        (lake,) = self.send("held").lakes
        transaction = self.send("begin", LakeHandle(lake))
        self.send(
            "execute", transaction, StatementRequest(kind="statement", sql=sql, parameters=[])
        )
        self.send("commit", transaction)


def conflict() -> StoreError:
    """The store's refusal of a commit another writer got to first."""
    return StoreError(
        "STORE_TRANSACTION_CONFLICT",
        "Another writer changed the same data first; nothing was written.",
        {},
    )


def around(operation: str, wrapper: Callable[[Call], Any]) -> Callable[[], None]:
    """Every store call of `operation` goes through `wrapper`; the returned function undoes it."""
    real = Bound.call

    def call(self: Bound, name: str, envelope: object, /, *arguments: Any, **keywords: Any) -> Any:  # noqa: ANN401
        if self.contract.name != STORE or name != operation:
            return real(self, name, envelope, *arguments, **keywords)
        assert isinstance(envelope, CallEnvelope)
        proceed = lambda: real(self, name, envelope, *arguments, **keywords)  # noqa: E731
        return wrapper(Call(self, envelope, arguments, proceed, real))

    Bound.call = call  # type: ignore[method-assign]
    return lambda: setattr(Bound, "call", real)
