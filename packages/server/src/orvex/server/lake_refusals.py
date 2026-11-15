"""A store's refusal of a Direct Query lake's data, as the caller is told it (#447; ADR-0027 §3).

An existing DuckLake that does not pass its checks (no verified snapshot to serve, a file it
names outside its location, a catalog it can no longer read) is refused by the store with
`STORE_SOURCE_INVALID`. The caller is told so with that code (`semantic-error` v1), never as an
internal error: the connection's name, and for a Builder the store's reason. Nothing else of
the store's error reaches them (its message and details can name files and the DuckDB error).
Any other store error is not a refusal of the data: it stays an internal error.
"""

from __future__ import annotations

from orvex.contracts.semantic.errors import SemanticError
from orvex.store.errors import StoreError

CODE = "STORE_SOURCE_INVALID"
UNKNOWN = "checks_failed"  # the reason when the store gave none


def as_semantic(error: StoreError) -> SemanticError | None:
    """The caller's refusal for `error`, or None when it is not a refusal of a lake's data."""
    if error.code != CODE:
        return None
    connection = str(error.details.get("connection") or "") or "unnamed"
    reason = str(error.details.get("reason") or error.details.get("kind") or "") or UNKNOWN
    return SemanticError(CODE, {"connection": connection, "reason": reason})
