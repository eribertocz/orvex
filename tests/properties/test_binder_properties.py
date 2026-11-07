"""P2-28 (#367): the binder's fingerprints are invariant under spelling, and hidden members
never bind. No lake: these properties run on the compiled model alone."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from orvex.semantic.errors import SemanticError
from orvex.semantic.query.binding import BoundQuery, Limits, bind_query

from tests.properties.test_engine_oracle import EVERYTHING
from tests.properties.world import (
    COMPILED,
    HIDDEN_POLICY,
    PROFILE,
    Document,
    aggregate_queries,
    policies,
)

INSTANT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HIDDEN = policies(HIDDEN_POLICY)


def bind(document: object, hidden: bool = False) -> BoundQuery:
    def is_hidden(member: Any) -> bool:  # noqa: ANN401
        return hidden and HIDDEN.protection(member, ()) == "hidden"

    return bind_query(
        COMPILED,
        document if isinstance(document, str | bytes) else json.dumps(document),
        instant=INSTANT,
        limits=Limits(detail_page_size=1000),
        hidden=is_hidden,
    )


def _respelled(name: str, draw: st.DrawFn) -> str:
    """Another spelling of the same member: quoted parts, upper-cased when quoted."""
    if not draw(st.booleans()):
        return name
    parts = name.split(".")
    quoted = [f'"{part.upper()}"' if draw(st.booleans()) else part for part in parts]
    return ".".join(quoted)


@st.composite
def respellings(draw: st.DrawFn) -> tuple[Document, Document]:
    """One question twice: shuffled key order, and members in equivalent spellings."""
    document = draw(aggregate_queries())
    respelled = json.loads(json.dumps(document))  # a deep copy to change spellings in
    for entry in respelled.get("group_by", []):
        entry["member"] = _respelled(entry["member"], draw)
    for entry in respelled.get("filters", []):
        entry["member"] = _respelled(entry["member"], draw)
    order = draw(st.permutations(list(respelled)))
    shuffled = {key: respelled[key] for key in order}
    return document, shuffled


@PROFILE
@example((EVERYTHING, dict(reversed(list(EVERYTHING.items())))))
@given(respellings())
def test_the_fingerprint_is_invariant_under_key_order_and_spelling(
    pair: tuple[Document, Document],
) -> None:
    document, respelled = pair
    first = bind(document)
    second = bind(respelled)
    assert first.fingerprint == second.fingerprint
    assert first.canonical == second.canonical


@PROFILE
@example(EVERYTHING)
@given(aggregate_queries())
def test_binding_is_idempotent(document: Document) -> None:
    """The same text twice, and the text with whitespace changed, give byte-equal canonicals."""
    text = json.dumps(document)
    spaced = json.dumps(document, indent=3)
    assert bind(text).fingerprint == bind(text).fingerprint == bind(spaced).fingerprint
    assert bind(text).canonical == bind(spaced).canonical


@PROFILE
@given(aggregate_queries())
def test_a_bound_question_never_names_a_hidden_member(document: Document) -> None:
    naming = {
        **document,
        "group_by": [*document.get("group_by", []), {"member": "customers.segment"}],
    }
    with pytest.raises(SemanticError) as caught:
        bind(naming, hidden=True)
    assert caught.value.code == "MEMBER_NOT_FOUND"
    if not any(
        entry.get("member") == "customers.segment"
        for entry in [*document.get("group_by", []), *document.get("filters", [])]
    ):
        bound = bind(document, hidden=True)
        assert all("segment" not in member.name for member in bound.members)
