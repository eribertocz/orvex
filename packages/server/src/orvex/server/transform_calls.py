"""The server's calls to the transform interface contract (#486 step 3): the implementation the
installed manifests choose, reached through its in-process binding, each call with the envelope
of the command it belongs to.

`transform().<operation>(envelope("<operation>"), ...)` sends one call. A command opens
`calling(folder)` once for the project it acts on; every envelope inside comes from that
block's `CallContext` (`command_caller.project_context`: the operating-system user, the
project folder, its revision, one correlation id). A block inside another for the same project
keeps the outer one's context, so one command (a check, the run after it, its records) is
followed end to end under one id. Outside any block there is no context, and no envelope can be
made (CALL_CONTEXT_MISSING): no call is sent.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import cast

from orvex.contracts.binding import Bound
from orvex.contracts.context import CallContext
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.transform.interface import TRANSFORM, Transform
from orvex.server.assembly import implementation_of
from orvex.server.command_caller import project_context, project_id
from orvex.server.identity import load_identity

_CONTEXT: ContextVar[CallContext | None] = ContextVar("transform_calls", default=None)


class CallContextMissing(Exception):
    """A transform call was about to be made outside any command's `calling` block."""

    code = "CALL_CONTEXT_MISSING"


@contextmanager
def calling(folder: Path) -> Iterator[None]:
    """Calls to transform inside this block belong to one command on the project in `folder`
    (the enclosing block's command, when it is for the same project)."""
    current = _CONTEXT.get()
    if current is not None and current.project_id == project_id(folder):
        yield
        return
    token = _CONTEXT.set(project_context(folder))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def envelope(operation: str) -> CallEnvelope:
    """The envelope of one call of `operation` in the current command."""
    context = _CONTEXT.get()
    if context is None:
        raise CallContextMissing(
            f"{CallContextMissing.code}: transform.{operation} called outside a command"
        )
    return context.envelope(TRANSFORM, operation)


@functools.cache
def _bound() -> Bound:
    implementation = implementation_of(load_identity(), TRANSFORM.name)
    return Bound(TRANSFORM, implementation, serialized=False)


def transform() -> Transform:
    """The transform contract, served by the installed implementation through its in-process
    binding: each operation is checked as the contract's protocol declares it."""
    return cast("Transform", _bound())
