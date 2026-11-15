"""The store's own tests reaching its implementation as a consumer does: through the store
contract's in-process binding and its client (`orvex.contracts.store.client`)."""

from __future__ import annotations

from orvex.contracts.binding import Bound
from orvex.contracts.store.client import Caller, StoreClient
from orvex.contracts.store.interface import STORE
from orvex.store.api import Store

CALLER = Caller(actor="store-tests", project_id="store-tests", revision="1", correlation_id="t")


def store_client() -> StoreClient:
    """A client of a fresh `Store`."""
    return StoreClient(Bound(STORE, Store(), serialized=False), CALLER)
