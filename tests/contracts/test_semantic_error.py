"""`semantic-error` v1 (#277): the schema, `codes.json` and both message catalogs agree, every
template uses only fields it may show, and no detail can carry a data value."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tools.repository import REPOSITORY_ROOT, identity

CONTRACT = REPOSITORY_ROOT / "contracts" / "semantic-error" / "v1"
SCHEMA = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
CODES: dict[str, Any] = json.loads((CONTRACT / "codes.json").read_text(encoding="utf-8"))
MESSAGES = {
    locale: json.loads((CONTRACT / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
    for locale in ("es", "en")
}
DEFS: dict[str, Any] = SCHEMA["$defs"]
NEW_CODES = (
    "METRIC_ALIGNMENT_AMBIGUOUS",
    "METRIC_CYCLE",
    "CONTEXT_CONFLICT",
    "FILTER_CYCLE",
    "WINDOW_UNDEFINED_AT_TOTAL",
    "AS_KNOWN_AT_UNSUPPORTED",
    "PARAMETER_INVALID",
    "REAGGREGATION_NOT_ALLOWED",
    "QUERY_INVALID",
)
# The kinds of value a detail may hold. None of them is a data value, a secret or SQL.
DETAIL_TYPES = {
    "Name", "Names", "Names2", "Path", "Version", "Versions", "Count", "Problems", "QueryProblems",
    "PolicyProblems",
}  # fmt: skip
FORBIDDEN_FIELD_NAMES = re.compile(r"value|sample|row|sql|secret|password|token|predicate|mask")


def variants() -> list[dict[str, Any]]:
    return [DEFS[item["$ref"].removeprefix("#/$defs/")] for item in SCHEMA["oneOf"]]


def details_of(variant: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = DEFS[variant["properties"]["details"]["$ref"].removeprefix("#/$defs/")]
    return target


def placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def test_the_catalog_has_42_codes_and_every_file_lists_them_all() -> None:
    enumerated = set(DEFS["SemanticErrorCode"]["enum"])
    assert len(enumerated) == 42
    assert set(CODES) == enumerated
    assert {variant["properties"]["code"]["const"] for variant in variants()} == enumerated
    for locale, catalog in MESSAGES.items():
        assert set(catalog) == enumerated, locale


def test_the_8_codes_of_s7_to_s14_and_238_are_in_the_catalog() -> None:
    assert set(NEW_CODES) <= set(CODES)
    assert "MISSING_EXTENSION_DECLARATION" in CODES


@pytest.mark.parametrize("code", sorted(CODES))
def test_each_code_has_severity_category_details_disclosure_and_both_templates(code: str) -> None:
    entry = CODES[code]
    assert entry["severity"] in DEFS["Severity"]["enum"]
    assert entry["category"] in DEFS["Category"]["enum"]
    assert set(entry["details"].values()) <= {"public", "builder"}
    for locale in MESSAGES:
        assert set(MESSAGES[locale][code]) == {"generic", "builder"}, locale
        assert all(MESSAGES[locale][code].values()), locale
    mine = [v for v in variants() if v["properties"]["code"]["const"] == code]
    for variant in mine:
        assert variant["properties"]["severity"] == {"const": entry["severity"]}
        assert variant["properties"]["category"] == {"const": entry["category"]}
        assert set(variant["required"]) >= {"kind", "code", "severity", "category", "details"}
    builder_fields = {f for f, d in entry["details"].items() if d == "builder"}
    public_fields = set(entry["details"]) - builder_fields
    audiences = {json.dumps(v["properties"]["audience"]) for v in mine}
    if builder_fields:
        assert audiences == {'{"const": "builder"}', '{"const": "other"}'}
    else:
        assert audiences == {'{"$ref": "#/$defs/Audience"}'}
    for variant in mine:
        details = details_of(variant)
        assert details["additionalProperties"] is False
        builder_view = variant["properties"]["audience"] != {"const": "other"}
        expected = set(entry["details"]) if builder_view else public_fields
        assert set(details["properties"]) == expected
        assert set(details["required"]) == expected - set(entry["optional"])
    assert set(entry["optional"]) <= builder_fields, "only a builder field may be optional"


def test_only_the_budget_estimate_is_optional() -> None:
    # #238 rule 12: left out when the query reads a dataset with a row policy.
    assert {code: e["optional"] for code, e in CODES.items() if e["optional"]} == {
        "QUERY_BUDGET_EXCEEDED": ["estimate"]
    }


def test_severity_is_error_except_the_warning_and_the_notices() -> None:
    assert {code for code, e in CODES.items() if e["severity"] != "error"} == {
        "DEPRECATED_METRIC",
        "FILTER_INTERSECTION_EMPTY",
        "POLICY_ATTRIBUTE_MISSING",
        "POLICY_MAPPING_MISSING",
    }
    assert CODES["DEPRECATED_METRIC"]["severity"] == "warning"
    assert CODES["FILTER_INTERSECTION_EMPTY"]["severity"] == "notice"
    assert CODES["POLICY_ATTRIBUTE_MISSING"]["severity"] == "notice"
    assert CODES["POLICY_MAPPING_MISSING"]["severity"] == "notice"


@pytest.mark.parametrize("locale", ["es", "en"])
@pytest.mark.parametrize("code", sorted(CODES))
def test_templates_name_only_fields_their_audience_may_see(code: str, locale: str) -> None:
    disclosure = CODES[code]["details"]
    public = {f for f, d in disclosure.items() if d == "public"}
    templates = MESSAGES[locale][code]
    assert placeholders(templates["generic"]) <= public, "a generic template shows a builder field"
    assert placeholders(templates["builder"]) <= set(disclosure)
    for template in templates.values():
        for _, field, spec, conversion in string.Formatter().parse(template):
            assert field is None or (
                re.fullmatch(r"[a-z_]+", field) and not spec and not conversion
            )


def test_a_placeholder_outside_the_details_or_a_builder_field_in_a_generic_template_is_caught() -> (
    None
):
    disclosure = CODES["QUERY_BUDGET_EXCEEDED"]["details"]
    public = {f for f, d in disclosure.items() if d == "public"}
    assert not placeholders("Estimate {estimate}") <= public
    assert not placeholders("{nope}") <= set(disclosure)


@pytest.mark.parametrize("code", sorted(CODES))
def test_no_detail_can_carry_a_data_value(code: str) -> None:
    for variant in (v for v in variants() if v["properties"]["code"]["const"] == code):
        for name, field in details_of(variant)["properties"].items():
            assert not FORBIDDEN_FIELD_NAMES.search(name), name
            if "enum" in field:
                assert all(isinstance(value, str) for value in field["enum"])
            else:
                assert field["$ref"].removeprefix("#/$defs/") in DETAIL_TYPES, name


def test_masked_members_and_missing_policy_attributes_have_nothing_to_reveal() -> None:
    # A masked member's builder message shows only what the generic one shows.
    for locale in MESSAGES:
        templates = MESSAGES[locale]["MEMBER_MASKED"]
        assert placeholders(templates["builder"]) == placeholders(templates["generic"])
    assert set(CODES["MEMBER_MASKED"]["details"].values()) == {"public"}
    # A missing attribute or mapping never names the attribute, the mapping table or a value
    # (security.md §3.3), and since the Owner decision of 2026-11-02 (#304, #123) names the
    # policy only to a builder: everyone else gets one generic sentence.
    for code in ("POLICY_ATTRIBUTE_MISSING", "POLICY_MAPPING_MISSING"):
        assert CODES[code]["details"] == {"policy": "builder"}, code
        for locale in MESSAGES:
            assert placeholders(MESSAGES[locale][code]["generic"]) == set(), (code, locale)


def test_a_name_never_holds_a_control_character() -> None:
    validator = Draft202012Validator(DEFS["Name"])
    assert not validator.is_valid("a\u0000b")
    assert not validator.is_valid("line\nbreak")
    assert validator.is_valid("ventas.monto")


def test_no_template_holds_the_product_name() -> None:
    names = identity(REPOSITORY_ROOT)
    forbidden = {str(names["name"]).lower(), str(names["slug"]).lower()}
    for catalog in MESSAGES.values():
        for templates in catalog.values():
            for template in templates.values():
                assert not any(name in template.lower() for name in forbidden), template


def examples(kind: str) -> list[Path]:
    return sorted((CONTRACT / "examples" / kind).glob("*.json"))


def test_every_code_has_a_valid_builder_example_and_an_other_view_when_it_hides_fields() -> None:
    found = {
        (payload["code"], payload["audience"])
        for payload in (json.loads(p.read_text(encoding="utf-8")) for p in examples("valid"))
    }
    for code, entry in CODES.items():
        assert (code, "builder") in found, code
        if "builder" in entry["details"].values():
            assert (code, "other") in found, code


@pytest.mark.parametrize("code", NEW_CODES)
def test_each_new_code_has_an_invalid_example_with_a_missing_detail(code: str) -> None:
    path = (
        CONTRACT / "examples" / "invalid" / f"{code.lower().replace('_', '-')}-missing-detail.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["code"] == code
    assert set(payload["details"]) < set(CODES[code]["details"])
    assert not Draft202012Validator(SCHEMA).is_valid(payload)


@pytest.mark.parametrize("code", ["POLICY_ATTRIBUTE_MISSING", "POLICY_MAPPING_MISSING"])
def test_a_no_rows_notice_names_no_policy_outside_the_builder_view(code: str) -> None:
    """Owner decision of 2026-11-02 (#304, #123): a generic sentence for everyone else, the
    policy only in the builder view; the result still has zero rows."""
    from orvex.semantic import SemanticError

    notice = SemanticError(code, {"policy": "zone_rows_c4n4ry"})
    for locale in ("es", "en"):
        other = notice.to_json(locale, "other")
        assert "zone_rows_c4n4ry" not in other
        assert '"details":{}' in other
        assert "zone_rows_c4n4ry" in notice.to_json(locale, "builder")
