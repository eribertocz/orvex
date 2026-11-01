"""`SemanticError` (#277): validated when raised, rendered per locale and audience, never
disclosing a builder detail to any other audience."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from orvex.semantic import SemanticError

CODES: dict[str, Any] = json.loads(
    files("orvex.contracts.semantic_error").joinpath("v1_data", "codes.json").read_text("utf-8")
)
EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "semantic-error" / "v1" / "examples"


def valid_details(code: str) -> dict[str, object]:
    """The details of the contract's valid builder example for `code`."""
    path = EXAMPLES / "valid" / f"{code.lower().replace('_', '-')}.json"
    details: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))["details"]
    return details


@pytest.mark.parametrize("code", sorted(CODES))
@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("audience", ["other", "builder"])
def test_every_code_renders_in_both_languages_for_both_audiences(
    code: str, locale: str, audience: str
) -> None:
    error = SemanticError(code, valid_details(code))
    message = error.to_message(locale, audience).model_dump(mode="json", by_alias=True)
    assert message["locale"] == locale
    assert message["audience"] == audience
    assert message["severity"] == CODES[code]["severity"]
    assert message["message"]
    assert "{" not in message["message"]
    builder_fields = {f for f, d in CODES[code]["details"].items() if d == "builder"}
    if audience == "builder":
        assert set(message["details"]) == set(CODES[code]["details"])
    else:
        assert not set(message["details"]) & builder_fields


@pytest.mark.parametrize("audience", [None, "other", "viewer", "admin", "BUILDER", ""])
def test_any_audience_but_builder_sees_no_builder_field_or_value(audience: str | None) -> None:
    evidence = SemanticError(
        "EVIDENCE_VIOLATED",
        {
            "dataset": "dim_consumers",
            "evidence": "unique",
            "fields": ["consumer_secret_field"],
            "violations": "987654321",
        },
    )
    budget = SemanticError(
        "QUERY_BUDGET_EXCEEDED",
        {"budget": "rows_scanned", "limit": "1000000", "estimate": "424242424242"},
    )
    for error, hidden in (
        (evidence, ("consumer_secret_field", "987654321")),
        (budget, ("424242424242",)),
    ):
        text = error.to_json("en", audience)
        assert all(value not in text for value in hidden), text
        message = json.loads(text)
        assert message["audience"] == "other"
    assert json.loads(evidence.to_json("en", "builder"))["details"]["violations"] == "987654321"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        (None, "es"),
        ("es", "es"),
        ("en", "en"),
        ("en-US", "en"),
        ("fr", "es"),
        ("", "es"),
        ("EN", "en"),
    ],
)
def test_spanish_by_default_english_on_request_any_other_locale_gives_spanish(
    locale: str | None, expected: str
) -> None:
    error = SemanticError("MEMBER_NOT_FOUND", {"member": "ventas.monto"})
    assert error.to_message(locale).root.locale.root == expected


def test_the_message_says_what_failed_in_each_language() -> None:
    error = SemanticError("MEMBER_NOT_FOUND", {"member": "ventas.monto"})
    assert error.to_message("es").root.message == "No existe el miembro ventas.monto."
    assert error.to_message("en").root.message == "The member ventas.monto does not exist."


@pytest.mark.parametrize(
    ("code", "details", "reason"),
    [
        ("NO_SUCH_CODE", {}, "unknown semantic error code"),
        ("MEMBER_NOT_FOUND", {}, r"missing \['member'\]"),
        ("MEMBER_NOT_FOUND", {"member": "a", "value": "Acme"}, r"extra \['value'\]"),
        ("METRIC_CYCLE", {"cycle": []}, "cycle"),
        (
            "EVIDENCE_VIOLATED",
            {"dataset": "d", "evidence": "unique", "fields": ["f"], "violations": 37},
            "violations",
        ),
        ("MEMBER_MASKED", {"member": "m", "operation": "peek"}, "operation"),
    ],
)
def test_an_invalid_error_raises_before_rendering(
    code: str, details: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        SemanticError(code, details)


def test_a_value_is_inserted_as_text_and_never_read_as_a_template() -> None:
    error = SemanticError("MEMBER_NOT_FOUND", {"member": "{member.__class__}"})
    assert error.to_message("en").root.message == "The member {member.__class__} does not exist."


def test_the_same_input_gives_byte_identical_output() -> None:
    def make() -> str:
        return SemanticError(
            "AMBIGUOUS_RELATIONSHIP_PATH",
            {
                "from": "orders",
                "to": "customers",
                "roles": ["billing_customer", "shipping_customer"],
            },
        ).to_json("en", "builder")

    assert make() == make()
    assert make().encode() == make().encode()


def test_details_cannot_be_changed_after_the_error_is_raised() -> None:
    error = SemanticError("MEMBER_NOT_FOUND", {"member": "a"})
    with pytest.raises(TypeError):
        error.details["member"] = "b"  # type: ignore[index]


def test_problems_are_rendered_as_their_count() -> None:
    error = SemanticError(
        "OSSIE_INVALID",
        {
            "file": "semantic/sales.ossie.yaml",
            "problems": [
                {"pointer": "/datasets/0", "line": "3", "rule": "schema"},
                {"pointer": "/datasets/1", "line": "9", "rule": "reference"},
            ],
            "problems_omitted": "0",
        },
    )
    assert "2 problems" in error.to_message("en", "builder").root.message
    assert error.to_message("en").root.details.model_dump() == {"file": "semantic/sales.ossie.yaml"}


@given(st.text(max_size=40))
def test_any_requested_locale_renders_a_known_language(locale: str) -> None:
    message = SemanticError("MEMBER_NOT_FOUND", {"member": "a"}).to_message(locale)
    assert message.root.locale.root in {"es", "en"}


def test_a_look_alike_of_the_builder_audience_gets_the_other_view() -> None:
    class LooksLikeBuilder(str):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = str.__hash__

    error = SemanticError(
        "QUERY_BUDGET_EXCEEDED", {"budget": "rows_scanned", "limit": "10", "estimate": "424242"}
    )
    assert "424242" not in error.to_json("en", LooksLikeBuilder("other"))


def test_an_invalid_detail_is_named_in_a_short_message() -> None:
    with pytest.raises(ValueError, match=r"^MEMBER_MASKED: invalid details \['operation'\]$"):
        SemanticError("MEMBER_MASKED", {"member": "m", "operation": "peek"})


def test_an_estimate_left_out_for_a_row_policy_gives_the_generic_text_to_everyone() -> None:
    error = SemanticError("QUERY_BUDGET_EXCEEDED", {"budget": "rows_scanned", "limit": "1000000"})
    builder = error.to_message("en", "builder").root
    assert builder.details.model_dump(exclude_none=True) == {
        "budget": "rows_scanned",
        "limit": "1000000",
    }
    assert builder.message == error.to_message("en").root.message
    assert "estimate" not in builder.message


SCHEMA = json.loads((EXAMPLES.parent / "schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("code", sorted(CODES))
@pytest.mark.parametrize("audience", ["other", "builder"])
def test_every_rendered_message_is_valid_against_the_contract_schema(
    code: str, audience: str
) -> None:
    variants = [valid_details(code)]
    for optional in CODES[code]["optional"]:
        variants.append({k: v for k, v in valid_details(code).items() if k != optional})
    for details in variants:
        for locale in ("es", "en"):
            text = SemanticError(code, details).to_json(locale, audience)
            assert Draft202012Validator(SCHEMA).is_valid(json.loads(text)), text
