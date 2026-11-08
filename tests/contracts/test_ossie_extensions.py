"""`ossie-extensions` v1 (#276): the payload schema, its examples, the generated types and the
compatibility of its surface."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from orvex.contracts.ossie_extensions.v1 import OssieExtensionPayload
from pydantic import ValidationError

from tools.contracts.compat import incompatible
from tools.repository import REPOSITORY_ROOT

CONTRACT = REPOSITORY_ROOT / "contracts" / "ossie-extensions" / "v1"
SCHEMA = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
SURFACE = Path(__file__).with_name("ossie_extensions_v1_frozen.json")
BRANCHES = {
    "model": "ModelPayload",
    "dataset": "DatasetPayload",
    "field": "FieldPayload",
    "metric": "MetricPayload",
    "relationship": "RelationshipPayload",
}


def examples(kind: str) -> list[Path]:
    return sorted((CONTRACT / "examples" / kind).glob("*.json"))


def branch_errors(payload: dict[str, Any]) -> list[str]:
    """The errors of the element's own schema, where the precise reason is (the top-level union
    reports only that no branch matched)."""
    name = BRANCHES.get(str(payload.get("element")))
    target = {**SCHEMA, "oneOf": None, "$ref": f"#/$defs/{name}"} if name else SCHEMA
    target = {key: value for key, value in target.items() if value is not None}
    validator = Draft202012Validator(target)
    return [error.message for error in validator.iter_errors(payload)] + [
        sub.message for error in validator.iter_errors(payload) for sub in (error.context or [])
    ]


# The precise reason each invalid example is rejected for.
REASONS = {
    "amount-without-currency": "'currency' is a required property",
    "axes-on-additive": "False schema does not allow",
    "components-on-additive": "False schema does not allow",
    "currency-without-semantic-type": "'semantic_type' is a dependency of 'currency'",
    "keep-without-targets": "'targets' is a required property",
    "negative-tolerance": "'-0.01' does not match",
    "period-end-axis-without-period-end": "'period_end' is a required property",
    "period-end-without-period-end-axis": "does not contain items matching",
    "unit-on-amount": "False schema does not allow",
    "approximate-not-sketch": "False schema does not allow",
    "bridge-without-right": "'right' is a required property",
    "cardinality-many-to-many": "'many_to_many' is not one of",
    "cardinality-one-to-many": "'one_to_many' is not one of",
    "decimal-as-number": "1.0 is not of type 'string'",
    "deprecated-without-deprecation": "'deprecation' is a required property",
    "element-unknown": "is not valid under any of the given schemas",
    "enum-outside-list": "'secret' is not one of",
    "geo-path-not-geojson": "does not match",
    "geo-path-outside": "does not match",
    "geo-without-source": "'geo_source' is a required property",
    "malformed-currency": "'usd' does not match",
    "malformed-duration": "'1 hour' does not match",
    "malformed-zone": "'la paz' does not match",
    "missing-element": "is not valid under any of the given schemas",
    "missing-version": "'version' is a required property",
    "numeric-as-number": "18 is not of type 'string'",
    "over-metrics-and-window": "should not be valid under",
    "over-metrics-without-align": "'align' is a required property",
    "quantity-without-unit": "'unit' is a required property",
    "recomputable-without-components": "'components' is a required property",
    "semi-additive-without-axes": "'non_additive_axes' is a required property",
    "unknown-member": "Additional properties are not allowed ('colour'",
    "version-2": "1 was expected",
    "window-without-densify": "'densify' is a required property",
}


def test_every_invalid_example_has_its_reason() -> None:
    assert {path.stem for path in examples("invalid")} == set(REASONS)


@pytest.mark.parametrize("path", examples("invalid"), ids=lambda path: path.stem)
def test_each_invalid_example_is_rejected_for_its_reason(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(SCHEMA).iter_errors(payload)), "accepted"
    errors = branch_errors(payload)
    assert any(REASONS[path.stem] in error for error in errors), errors


@pytest.mark.parametrize("path", examples("valid"), ids=lambda path: path.stem)
def test_each_valid_example_is_accepted_by_its_element(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert branch_errors(payload) == []


def test_the_valid_examples_cover_every_element_and_capability() -> None:
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in examples("valid")]
    assert {payload["element"] for payload in payloads} == set(BRANCHES)
    present = {key for payload in payloads for key in payload}
    for member in (
        "geo_source",
        "preaggregations",
        "over_metrics",
        "window",
        "parameters",
        "segments",
        "calculated",
        "cohorts",
        "funnels",
        "history",
        "bridge",
        "numeric",
        "time_zone",
    ):
        assert member in present, member


@pytest.mark.parametrize("path", examples("valid"), ids=lambda path: path.stem)
def test_the_generated_python_type_round_trips_each_example(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    parsed = OssieExtensionPayload.model_validate_json(text)
    assert parsed.model_dump(mode="json", exclude_none=True, by_alias=True) == payload


# The generated types carry the structure; the schema alone carries the conditional rules
# (`if`/`then`, `dependentRequired`, `not`), so a payload is validated against the schema before it
# is parsed (P2-03). These structural cases are refused by the types too.
STRUCTURAL = sorted(
    set(REASONS)
    - {
        "amount-without-currency",
        "approximate-not-sketch",
        "deprecated-without-deprecation",
        "geo-without-source",
        "missing-element",
        "missing-version",
        "over-metrics-and-window",
        "quantity-without-unit",
        "recomputable-without-components",
        "semi-additive-without-axes",
        "axes-on-additive",
        "components-on-additive",
        "currency-without-semantic-type",
        "keep-without-targets",
        "period-end-axis-without-period-end",
        "period-end-without-period-end-axis",
        "unit-on-amount",
    }
)


@pytest.mark.parametrize("name", STRUCTURAL)
def test_the_generated_python_type_refuses_each_structural_violation(name: str) -> None:
    text = (CONTRACT / "examples" / "invalid" / f"{name}.json").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        OssieExtensionPayload.model_validate_json(text)


def test_decimal_text_keeps_its_exact_digits() -> None:
    text = (CONTRACT / "examples" / "valid" / "dataset-bridge.json").read_text(encoding="utf-8")
    dumped = OssieExtensionPayload.model_validate_json(text).model_dump_json(exclude_none=True)
    assert '"total":"1.00"' in dumped
    assert '"tolerance":"0.00"' in dumped


def test_the_schema_never_names_a_vendor() -> None:
    # The vendor token comes from identity.toml at run time (P2-03).
    text = json.dumps(SCHEMA)
    assert "vendor_name" not in text


# --- Compatibility of v1 ---------------------------------------------------------------------
#
# `ossie_extensions_v1_frozen.json` is the schema as first released. Every node of it must still
# exist in the current schema and accept at least what it accepted: the only allowed changes are a
# new optional member, a new enum value, a new `$defs` entry and documentation. Below `if` and
# `not` a looser subschema makes the schema stricter, so there nothing may change at all.
# The checker is `tools/contracts/compat.py`, shared with the upstream watch (#285).

FROZEN = json.loads(SURFACE.read_text(encoding="utf-8"))


def test_v1_is_unchanged_or_compatibly_extended() -> None:
    assert incompatible(FROZEN, SCHEMA) == []


def rule_index(conditions: list[dict[str, Any]], keyword: str, member: str) -> int:
    """The position of the `keyword` rule that names `member` among a payload's conditions."""
    return next(
        index
        for index, rule in enumerate(conditions)
        if keyword in rule and member in json.dumps(rule[keyword])
    )


def mutated(change: str) -> dict[str, Any]:
    schema: dict[str, Any] = copy.deepcopy(SCHEMA)
    defs = schema["$defs"]
    conditions = defs["MetricPayload"]["allOf"]
    if change == "remove-member":
        del defs["FieldPayload"]["properties"]["pii"]
    elif change == "retype-member":
        defs["FieldPayload"]["properties"]["calendar"] = {"type": "integer"}
    elif change == "require-member":
        defs["FieldPayload"]["required"].append("pii")
    elif change == "remove-enum-value":
        defs["FieldPayload"]["properties"]["pii"]["enum"].remove("sensitive")
    elif change == "remove-nested-enum-value":
        defs["Axis"]["oneOf"][0]["properties"]["behavior"]["enum"].remove("additive")
    elif change == "remove-items-enum-value":
        defs["DatasetPayload"]["properties"]["evidence"]["items"]["enum"].remove("append_only")
    elif change == "retype-nested":
        defs["MetricPayload"]["properties"]["allocation"]["properties"]["bridge"] = {
            "type": "integer"
        }
    elif change == "tighten-pattern":
        defs["Decimal"]["pattern"] = "^[0-9]+$"
    elif change == "new-conditional":
        defs["FieldPayload"]["allOf"].append(
            {"if": {"required": ["pii"]}, "then": {"required": ["calendar"]}}
        )
    elif change == "new-dependency":
        defs["FieldPayload"]["dependentRequired"]["pii"] = ["calendar"]
    elif change == "new-required-member":
        defs["FieldPayload"]["properties"]["owner"] = {"type": "string"}
        defs["FieldPayload"]["required"].append("owner")
    elif change == "new-optional-member":
        defs["FieldPayload"]["properties"]["owner"] = {"type": "string"}
    elif change == "new-enum-value":
        defs["FieldPayload"]["properties"]["pii"]["enum"].append("secret")
    elif change == "new-definition":
        defs["Unused"] = {"type": "string"}
    elif change == "loosen-not":
        conditions[rule_index(conditions, "not", "window")]["not"]["required"].remove("window")
    elif change == "widen-if":
        # The rule that forbids the axes members unless the metric is semi-additive.
        widened = next(
            rule["if"]["properties"]["additivity"]["enum"]
            for rule in conditions
            if "enum" in rule.get("if", {}).get("properties", {}).get("additivity", {})
            and "semi_additive" not in rule["if"]["properties"]["additivity"]["enum"]
        )
        widened.append("semi_additive")
    elif change == "constrain-in-then":
        rule = next(rule for rule in conditions if "then" in rule and "properties" in rule["then"])
        rule["then"]["properties"]["unit"] = False
    elif change == "documentation":
        defs["FieldPayload"]["description"] = "reworded"
    return schema


INCOMPATIBLE = [
    "remove-member",
    "retype-member",
    "require-member",
    "remove-enum-value",
    "remove-nested-enum-value",
    "remove-items-enum-value",
    "retype-nested",
    "tighten-pattern",
    "new-conditional",
    "new-dependency",
    "new-required-member",
    "loosen-not",
    "widen-if",
    "constrain-in-then",
]


@pytest.mark.parametrize("change", INCOMPATIBLE)
def test_an_incompatible_change_is_caught(change: str) -> None:
    assert incompatible(SCHEMA, mutated(change)), change


@pytest.mark.parametrize(
    "change", ["new-optional-member", "new-enum-value", "new-definition", "documentation"]
)
def test_a_compatible_change_passes(change: str) -> None:
    assert incompatible(SCHEMA, mutated(change)) == []
