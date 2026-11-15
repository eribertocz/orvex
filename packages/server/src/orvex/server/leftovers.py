"""Leftover tables (#88): the lake tables no model stores now, and the statements that drop
them, both decided by transform from the tables the lake lists."""

from __future__ import annotations

from collections.abc import Sequence

from orvex.contracts.store.v1 import StatementRequest
from orvex.contracts.transform.models import Model
from orvex.server.transform_calls import envelope, transform
from orvex.store.lake import Lake


def _leftovers(lake: Lake, models: Sequence[Model]) -> list[tuple[str, str]]:
    """Tables of the lake that no model stores now, in schemas a model could use (#88)."""
    return list(
        transform().leftovers(envelope("leftovers"), tuple(sorted(lake.tables())), tuple(models))
    )


def _drop_statements(leftovers: Sequence[tuple[str, str]]) -> list[StatementRequest]:
    """Drop each leftover and forget its watermark (a model added back later starts over)."""
    return list(transform().leftover_statements(envelope("leftover_statements"), tuple(leftovers)))
