"""The server's calls to the transform interface contract (#486 step 3): the implementation the
installed manifests choose, reached through its in-process binding, each call with a complete
envelope.

`transform().<operation>(envelope(), ...)` sends one call. The envelope names the project of
the `calling(folder)` block it is sent in, by the project folder's absolute path, with that
block's correlation id: a block inside another for the same project keeps the outer one's, so
one command (a check, the run after it, its records) is followed end to end under one id. The
actor is the local Builder who started the command (or the scheduler they started), and the
revision is the project's files as they are now. Outside a `calling` block the envelope has no
project, and the binding refuses the call (CONTRACT_ENVELOPE_INVALID) before transform runs:
fail closed.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from orvex.contracts.binding import Bound
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.transform.interface import TRANSFORM, Transform
from orvex.server.assembly import implementation_of
from orvex.server.identity import load_identity

ACTOR = "local-builder"
REVISION = "working-tree"


@dataclass(frozen=True, slots=True)
class _Context:
    project_id: str
    correlation_id: str


_CONTEXT: ContextVar[_Context | None] = ContextVar("transform_calls", default=None)


@contextmanager
def calling(folder: Path) -> Iterator[None]:
    """Calls to transform inside this block act on the project in `folder`, under one
    correlation id (the enclosing block's, when it is for the same project)."""
    project_id = str(Path(folder).expanduser().absolute())
    current = _CONTEXT.get()
    if current is not None and current.project_id == project_id:
        yield
        return
    token = _CONTEXT.set(_Context(project_id, str(uuid.uuid4())))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@functools.cache
def _bound() -> Bound:
    implementation = implementation_of(load_identity(), TRANSFORM.name)
    return Bound(TRANSFORM, implementation, serialized=False)


def envelope() -> CallEnvelope:
    """The envelope of one call from here: blank project and correlation outside `calling`."""
    context = _CONTEXT.get()
    return CallEnvelope(
        contract_name=TRANSFORM.name,
        contract_version=str(TRANSFORM.version),
        correlation_id="" if context is None else context.correlation_id,
        actor=ACTOR,
        project_id="" if context is None else context.project_id,
        revision=REVISION,
    )


def transform() -> Transform:
    """The transform contract, served by the installed implementation through its in-process
    binding: each operation is called with `envelope()` first, and checked as the contract's
    protocol declares it."""
    return cast("Transform", _bound())
