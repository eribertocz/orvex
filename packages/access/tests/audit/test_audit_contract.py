"""#437 AC1, R1, AC6: the `audit-event` v1 contract has an example for every kind, the package
reads the contract's own schema, the canonical form is RFC 8785 for the values an event holds,
and a planted secret or value never fits a `details` schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from orvex.access.audit.canonical import NotInProfile, canonical

from tools.repository import REPOSITORY_ROOT

CONTRACT = REPOSITORY_ROOT / "contracts/audit-event/v1"
VENDOR = REPOSITORY_ROOT / "vendor/json-canonicalization"
SCHEMA = json.loads((CONTRACT / "schema.json").read_text("utf-8"))


def test_the_package_reads_the_contracts_own_schema() -> None:
    packaged = REPOSITORY_ROOT / "packages/access/src/orvex/access/audit-event-v1.json"
    assert packaged.read_bytes() == (CONTRACT / "schema.json").read_bytes()


def test_every_kind_has_a_valid_example() -> None:
    kinds = set(SCHEMA["properties"]["kind"]["enum"])
    examples = {
        json.loads(path.read_text("utf-8"))["kind"]
        for path in (CONTRACT / "examples/valid").glob("*.json")
    }
    assert examples == kinds


def test_every_kind_has_a_closed_details_schema() -> None:
    rules = {rule["if"]["properties"]["kind"]["const"] for rule in SCHEMA["allOf"]}
    assert rules == set(SCHEMA["properties"]["kind"]["enum"])
    for rule in SCHEMA["allOf"]:
        target = rule["then"]["properties"]["details"]["$ref"].rsplit("/", 1)[1]
        assert SCHEMA["$defs"][target]["additionalProperties"] is False


@pytest.mark.parametrize(
    "details",
    [
        {"method": "password", "password": "hunter2"},
        {"method": "password", "token": "eyJhbGciOiJFZERTQSJ9"},
        {"method": "password", "secret": "s3cr3t"},
        {"method": "password", "value": "30.75"},
        {"method": "password", "rows": [["Z1", "30.75"]]},
    ],
)
def test_a_planted_secret_or_value_fits_no_details_schema(details: dict[str, object]) -> None:
    event = json.loads((CONTRACT / "examples/valid/auth-login.json").read_text("utf-8"))
    event["details"] = details
    assert list(Draft202012Validator(SCHEMA).iter_errors(event))


def test_r1_an_unknown_kind_is_refused_by_the_contract() -> None:
    event = json.loads((CONTRACT / "examples/valid/auth-logout.json").read_text("utf-8"))
    event["kind"] = "user.renamed"
    assert list(Draft202012Validator(SCHEMA).iter_errors(event))


def vendored_pairs() -> list[tuple[str, Path, Path]]:
    return [
        (path.name, path, VENDOR / "output" / path.name)
        for path in sorted((VENDOR / "input").glob("*.json"))
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
