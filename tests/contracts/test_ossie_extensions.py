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

from tools.repository import REPOSITORY_ROOT

CONTRACT = REPOSITORY_ROOT / "contracts" / "ossie-extensions" / "v1"
SCHEMA = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
SURFACE = Path(__file__).with_name("ossie_extensions_v1_surface.json")
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


# --- Compatibility of the v1 surface -----------------------------------------------------------


def surface(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every member path of every `$defs` object: its type or reference, whether it is required,
    and its enum values."""
    found: dict[str, dict[str, Any]] = {}
    for name, definition in schema["$defs"].items():
        for member, body in (definition.get("properties") or {}).items():
            found[f"{name}.{member}"] = {
                "type": body.get("type") or body.get("$ref") or body.get("const", "any"),
                "required": member in definition.get("required", []),
                "enum": sorted(body.get("enum", [])),
            }
    return found


def incompatible(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> list[str]:
    problems = []
    for path, before in old.items():
        after = new.get(path)
        if after is None:
            problems.append(f"{path} removed or renamed")
            continue
        if after["type"] != before["type"]:
            problems.append(f"{path} retyped")
        if after["required"] and not before["required"]:
            problems.append(f"{path} became required")
        if set(before["enum"]) - set(after["enum"]):
            problems.append(f"{path} lost enum values")
    for path, after in new.items():
        if path not in old and after["required"]:
            problems.append(f"{path} is a new required member")
    return problems


def test_the_v1_surface_is_unchanged_or_compatibly_extended() -> None:
    recorded = json.loads(SURFACE.read_text(encoding="utf-8"))
    assert incompatible(recorded, surface(SCHEMA)) == []


def mutated(change: str) -> dict[str, Any]:
    schema: dict[str, Any] = copy.deepcopy(SCHEMA)
    field = schema["$defs"]["FieldPayload"]
    if change == "remove":
        del field["properties"]["pii"]
    elif change == "retype":
        field["properties"]["calendar"] = {"type": "integer"}
    elif change == "require":
        field["required"].append("pii")
    elif change == "enum":
        field["properties"]["pii"] = {"enum": ["none", "personal"]}
    elif change == "new-required":
        field["properties"]["owner"] = {"type": "string"}
        field["required"].append("owner")
    elif change == "new-optional":
        field["properties"]["owner"] = {"type": "string"}
    return schema


@pytest.mark.parametrize("change", ["remove", "retype", "require", "enum", "new-required"])
def test_an_incompatible_change_is_caught(change: str) -> None:
    assert incompatible(surface(SCHEMA), surface(mutated(change)))


def test_an_added_optional_member_is_compatible() -> None:
    assert incompatible(surface(SCHEMA), surface(mutated("new-optional"))) == []
