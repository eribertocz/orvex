"""What a kit case calls an implementation through (#486 G1, G3): its binding, with a complete
envelope made for each call at the run's consumer version.

`client.<operation>(...)` sends a new envelope, with a fresh idempotency key when the operation
changes state; `client.envelope(operation)` and `client.send(operation, envelope, ...)` let a
case send one envelope twice (the idempotency check) or a changed one.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable
from typing import Any

from .binding import Bound
from .envelope import CallEnvelope
from .version import Version


class KitClient:
    """One case's caller: a consumer at `consumer` reaching the implementation through `bound`."""

    __slots__ = ("_bound", "_case", "_consumer", "_keys")

    def __init__(self, bound: Bound, consumer: Version, case: str) -> None:
        self._bound = bound
        self._consumer = consumer
        self._case = case
        self._keys = 0

    @property
    def consumer(self) -> Version:
        return self._consumer

    def envelope(self, operation: str, **changes: Any) -> CallEnvelope:  # noqa: ANN401
        """A complete envelope for one call of `operation`, with `changes` applied (a case may
        plant any value, a missing field included)."""
        key = None
        if self._bound.contract.operation(operation).changes_state:
            self._keys += 1
            key = f"kit-{self._case}-{self._keys}"
        envelope = CallEnvelope(
            contract_name=self._bound.contract.name,
            contract_version=str(self._consumer),
            correlation_id=f"kit-{self._case}",
            actor="kit-actor",
            project_id="kit-project",
            revision="kit-revision",
            idempotency_key=key,
        )
        return dataclasses.replace(envelope, **changes)

    def send(
        self, operation: str, envelope: object, /, *arguments: object, **keywords: object
    ) -> Any:  # noqa: ANN401 - the operation's result
        return self._bound.call(operation, envelope, *arguments, **keywords)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if not self._bound.contract.offers(name):
            raise AttributeError(f"contract {self._bound.contract.name} has no operation {name!r}")
        return functools.partial(self._new, name)

    def _new(self, operation: str, /, *arguments: object, **keywords: object) -> Any:  # noqa: ANN401
        return self.send(operation, self.envelope(operation), *arguments, **keywords)
