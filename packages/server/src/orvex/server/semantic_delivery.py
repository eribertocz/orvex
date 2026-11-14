"""The one delivery step of every semantic answer (#445; #444 precision 3): a read (`miss`), a
cache `hit` or `bypass`, or an asynchronous job's result. Nothing of an answer leaves before it.

For a synchronous question, `answered`:
1. takes the caller's concurrency slot before the question waits for its turn or is planned;
2. asks (`semantic_http.ask`) and builds the **whole** response in memory (JSON or Arrow);
3. `deliver`: revalidates and audits;
4. sends the response only if step 3 passed, and releases the slot in every outcome.

`deliver` (Owner precision 4):
- **Revalidate.** The session is checked again (server mode), and the caller's context is built
  again from the directory and the lake as they are now. A membership gone or a deactivated
  user refuses as at the request. A role changed since to one without the explorer permission
  is `ACCESS_FORBIDDEN` (who may ask at all is the request rules', at the request). A
  permission fingerprint other than the one the answer was computed under is
  `QUERY_RESULT_WITHDRAWN`.
- **The decision point** is that read: a revocation committed before it is honored; one
  committed after it applies to the next request, as for any request already authorized.
- **Audit** (`semantic_audit`): the `query` event of its stage, and #443's events of a withheld
  answer, at the stage that computed it (never again at a later delivery). Then the response
  goes.
"""

from __future__ import annotations

from collections.abc import Callable

from orvex.access.errors import ACCESS_FORBIDDEN, AccessError
from orvex.semantic.errors import SemanticError
from orvex.server import semantic_http
from orvex.server.route_permissions import allows
from orvex.server.semantic_audit import record_query, record_withheld


def deliver(
    semantic: semantic_http.Semantic,
    request: object,
    asked: semantic_http.Asked,
    stage: str,
    revalidate: Callable[[], None] | None = None,
    job: str | None = None,
) -> None:
    """Revalidate `asked` for `request` now, then record its audit events; raise to refuse."""
    if revalidate is not None:
        revalidate()  # the session, as the request rules read it
    now = semantic_http.current_context(semantic, request, asked.snapshot)
    role = now.get("role")
    if role != asked.context.get("role") and not allows("explorer", str(role)):
        raise AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")  # lowered since
    if now["permission_fingerprint"] != asked.context["permission_fingerprint"]:
        raise SemanticError("QUERY_RESULT_WITHDRAWN", {"reason": "permissions_changed"})
    if semantic.audit is None:
        return  # local mode: no audit log
    record_query(semantic.audit, asked.found, asked.context, stage, job)
    if stage != "delivered":  # a withheld answer's events, once, where it was computed
        record_withheld(semantic.audit, asked.found, asked.context)


def answered[R](
    semantic: semantic_http.Semantic,
    body: bytes,
    request: object,
    respond: Callable[[semantic_http.Asked], R],
    revalidate: Callable[[], None] | None = None,
) -> R:
    """A synchronous question, its slot held from before its turn until it is delivered."""
    slot = semantic_http.take_slot(semantic, request)
    try:
        asked = semantic_http.ask(semantic, body, request)
        response = respond(asked)  # the whole response, before anything is decided
        deliver(semantic, request, asked, "synchronous", revalidate)
        return response
    finally:
        slot.release()
