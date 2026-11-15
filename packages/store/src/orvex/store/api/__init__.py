"""The store's one public face (#486 §2 item 1): `Store`, its implementation of the interface
contract `orvex.contracts.store.interface.STORE`; everything else in the module is internal."""

from orvex.store.api.service import Store

__all__ = ["Store"]
