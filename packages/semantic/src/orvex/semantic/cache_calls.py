"""How a question reaches the result cache the server composed (#486 G1, §2 item 6): only through
the result-cache contract's binding, each call with its envelope, so whichever implementation
was chosen is held to the contract and an incomplete envelope never reaches it.

The envelope names the caller of the question's security context (`user`), its project and,
as the revision acted on, the compiled model's fingerprint. Each question has its own
correlation id; a `put` carries an idempotency key of its own.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from orvex.contracts.binding import Bound
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.semantic.interface import RESULT_CACHE, ResultCache, Stored, Use


class CacheCalls:
    """One question's calls of the composed cache."""

    __slots__ = ("_bound", "_envelope")

    def __init__(self, cache: ResultCache, context: Mapping[str, Any], revision: str) -> None:
        self._bound = Bound(RESULT_CACHE, cache, serialized=False)
        self._envelope = CallEnvelope(
            contract_name=RESULT_CACHE.name,
            contract_version=str(RESULT_CACHE.version),
            correlation_id=secrets.token_hex(16),
            actor=str(context["user"]),
            project_id=str(context["project"]),
            revision=revision,
        )

    def get(self, use: Use) -> Stored | None:
        found: Stored | None = self._bound.call("get", self._envelope, use)
        return found

    def put(self, use: Use, stored: Stored) -> None:
        key = f"{self._envelope.correlation_id}-put"
        self._bound.call("put", replace(self._envelope, idempotency_key=key), use, stored)
