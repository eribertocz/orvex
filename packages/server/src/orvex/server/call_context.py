"""The call context of each server entry point (#486 §2 item 6): one correlation id per HTTP
request, CLI command or scheduled run, and the `CallContext` every contract call of it carries.

An HTTP request keeps the caller's `X-Request-Id` when it is a well-formed id (8 to 128 letters,
digits, `.`, `_` or `-`) and gets a new one otherwise, so the id that answers, logs and calls
carry is one and the same. A command or a scheduled run gets a new id. The actor, project and
revision come from the entry point, which resolves them; `CallContext` refuses a missing or
placeholder value (CONTRACT_CONTEXT_INVALID).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping

from fastapi import Request
from orvex.contracts.context import CallContext

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,128}")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def request_correlation_id(headers: Mapping[str, str]) -> str:
    """The caller's request id when it is well formed, a new one otherwise."""
    given = headers.get(REQUEST_ID_HEADER)
    return given if given is not None and REQUEST_ID.fullmatch(given) else new_correlation_id()


def request_context(request: Request, actor: str, project_id: str, revision: str) -> CallContext:
    """The context of the contract calls one HTTP request makes, under its request id."""
    correlation: str = request.state.request_id
    return CallContext(correlation, actor, project_id, revision)


def command_context(actor: str, project_id: str, revision: str) -> CallContext:
    """The context of the contract calls one CLI command or scheduled run makes."""
    return CallContext(new_correlation_id(), actor, project_id, revision)
