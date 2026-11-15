"""#437 AC6: the canonical form of an event is RFC 8785 for every value an event holds, checked
on the RFC's vectors (carried in `fixtures/`, equal to the vendored ones), and a value outside
the profile is refused, never approximated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from orvex.access.audit.canonical import NotInProfile, canonical

VECTORS = Path(__file__).parent.parent / "fixtures/json-canonicalization"


def vendored_pairs() -> list[tuple[str, Path, Path]]:
    return [
        (path.name, path, VECTORS / "output" / path.name)
        for path in sorted((VECTORS / "input").glob("*.json"))
    ]


def test_the_canonical_form_is_rfc_8785_for_every_value_an_event_holds() -> None:
    """Every vendored RFC 8785 case within the profile gives the upstream bytes exactly; every
    case outside it (floats, large numbers) is refused, never approximated."""
    within = 0
    for _, given, expected in vendored_pairs():
        document = json.loads(given.read_text("utf-8"))
        try:
            produced = canonical(document)
        except NotInProfile:
            continue
        assert produced == expected.read_bytes().rstrip(b"\n"), given.name
        within += 1
    assert within == 2  # arrays and unicode; the others hold floats or non-ASCII names


@pytest.mark.parametrize("value", [0.5, 2**53, "\ud800", {"é": 1}, b"bytes"])
def test_a_value_outside_the_profile_is_refused(value: object) -> None:
    with pytest.raises(NotInProfile):
        canonical({"value": value})
