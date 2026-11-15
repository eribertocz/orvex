"""How a question reaches the result cache the server composed (#486 G1, §2 item 6): only through
the result-cache contract's binding, each call with the envelope of the entry point's call
context, so whichever implementation was chosen is held to the contract and an incomplete
envelope never reaches it. A cache composed without a call context is refused before any call
(fail closed).
"""

from __future__ import annotations

from orvex.contracts.binding import Bound
from orvex.contracts.context import CallContext
from orvex.contracts.semantic.errors import PlanError
from orvex.contracts.semantic.interface import RESULT_CACHE, ResultCache, Stored, Use


class CacheCalls:
    """One question's calls of the composed cache, under the entry point's call context."""

    __slots__ = ("_bound", "_call")

    def __init__(self, cache: ResultCache, call: CallContext | None) -> None:
        if call is None:
            raise PlanError("a result cache is composed without the call context of its caller")
        self._bound = Bound(RESULT_CACHE, cache, serialized=False)
        self._call = call

    def get(self, use: Use) -> Stored | None:
        found: Stored | None = self._bound.call(
            "get", self._call.envelope(RESULT_CACHE, "get"), use
        )
        return found

    def put(self, use: Use, stored: Stored) -> None:
        # One put per question: its correlation id names the effect.
        key = f"{self._call.correlation_id}-put"
        envelope = self._call.envelope(RESULT_CACHE, "put", idempotency_key=key)
        self._bound.call("put", envelope, use, stored)
