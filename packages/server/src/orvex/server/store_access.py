"""How the server reaches the store: a `StoreClient` on the installed implementation of the store
contract (`assembly.provider`), for the server's own caller unless a caller is given."""

from __future__ import annotations

import functools
import uuid

from orvex.contracts.store.client import Caller, StoreClient
from orvex.server.assembly import provider
from orvex.server.identity import Identity, load_identity

STORE_CONTRACT = "store"


@functools.cache
def _identity() -> Identity:
    return load_identity()


def server_caller() -> Caller:
    """The server acting for itself: its own actor, the instance and its current revision."""
    return Caller(
        actor=f"{_identity().slug}-server",
        project_id="instance",
        revision="current",
        correlation_id=uuid.uuid4().hex,
    )


def store(caller: Caller | None = None) -> StoreClient:
    """The store for `caller` (the server's own by default)."""
    return StoreClient(provider(_identity(), STORE_CONTRACT), caller or server_caller())
