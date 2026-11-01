"""P2-03 (#278): the Ossie adapter's case matrix. Every rejection row runs on the same model
written as YAML and as JSON, and must give the same code, rule, pointer and line."""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.ossie import documents
from orvex.semantic.ossie.model import OssieModel
from orvex.semantic.ossie.reader import read

ROOT = Path(__file__).resolve().parents[3]
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
CURRENT: str = _PRODUCT["ossie_vendor_name"]
PREVIOUS: str = _PRODUCT["previous_vendor_names"][0]
VENDORS = (CURRENT, *_PRODUCT["previous_vendor_names"])
CANARY = "zz9canary"

Model = dict[str, Any]


def field(name: str, **more: str) -> Model:
    return {"name": name, "expression": {"sql": name}, **more}


def entry(vendor: str, payload: object) -> Model:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return {"vendor_name": vendor, "data": data}


def base() -> Model:
    return {
        "version": "0.2.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "fields": [
                    field("sale_id", datatype="Integer"),
                    field("customer_id"),
                    field("amount", datatype="Decimal"),
                    field("sold_at", datatype="DateTime"),
                    field("note", datatype="String"),
                ],
                "metrics": [{"name": "sale_count", "expression": {"sql": "COUNT(sale_id)"}}],
            },
            {
                "name": "customers",
                "source": "customers",
                "primary_key": ["customer_id"],
                "fields": [field("customer_id"), field("region")],
            },
        ],
        "relationships": [
            {
                "name": "sale_customer",
                "from": "sales",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            }
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}}],
    }


def as_yaml(model: Model) -> bytes:
    return yaml.safe_dump(model, sort_keys=False, allow_unicode=True).encode()


def as_json(model: Model) -> bytes:
    return json.dumps(model, indent=2, ensure_ascii=False).encode()


def load(content: bytes, form: str, engines: Mapping[str, str] | None = None) -> OssieModel:
    return read(content, f"semantic/shop.ossie.{form}", VENDORS, engines)


def failure(content: bytes, form: str, engines: Mapping[str, str] | None = None) -> SemanticError:
    with pytest.raises(SemanticError) as caught:
        load(content, form, engines)
    return caught.value


def line_of(content: bytes, path: tuple[str | int, ...]) -> int:
    """The line our reader should report, found independently with PyYAML's node marks (JSON
    is YAML too): a member's line is its key's, an item's line is its value's."""
    node = yaml.compose(content.decode())
    line = node.start_mark.line + 1
    for part in path:
        if isinstance(node, yaml.MappingNode):
            key, node = next((k, v) for k, v in node.value if k.value == part)
            line = key.start_mark.line + 1
        else:
            node = node.value[part]
            line = node.start_mark.line + 1
    return int(line)


def problems(error: SemanticError) -> list[dict[str, str]]:
    return cast(list[dict[str, str]], error.details["problems"])


def both(*changes: Callable[[Model], None]) -> Callable[[Model], None]:
    def change(model: Model) -> None:
        for one in changes:
            one(model)

    return change


def pointer_path(pointer: str) -> tuple[str | int, ...]:
    parts = pointer.split("/")[1:]
    return tuple(int(part) if part.isdigit() else part for part in parts)


# --- the matrix -------------------------------------------------------------------------------

Change = Callable[[Model], None]


def ds(model: Model, name: str = "sales") -> Model:
    found: Model = next(d for d in model["datasets"] if d["name"] == name)
    return found


def fld(model: Model, name: str) -> Model:
    found: Model = next(f for f in ds(model)["fields"] if f["name"] == name)
    return found


def put(node: Model, payload: object, vendor: str = CURRENT) -> None:
    node.setdefault("custom_extensions", []).append(entry(vendor, payload))


def rel(model: Model) -> Model:
    found: Model = model["relationships"][0]
    return found


ROWS: list[tuple[str, Change, str, str, str]] = [
    # id, change, code, rule, pointer
    ("version-number", lambda m: m.update(version=0.2), "OSSIE_INVALID", "schema", "/version"),
    ("version-missing", lambda m: m.pop("version"), "OSSIE_INVALID", "schema", ""),
    ("unknown-top-level", lambda m: m.update({CANARY: 1}), "OSSIE_INVALID", "schema", ""),
    (
        "duplicate-dataset",
        lambda m: m["datasets"].append({**copy.deepcopy(ds(m)), "name": "SALES"}),
        "OSSIE_INVALID",
        "duplicate_name",
        "/datasets/2/name",
    ),
    (
        "field-and-metric-share-a-name",
        lambda m: ds(m)["metrics"].append({"name": "Note", "expression": {"sql": "COUNT(note)"}}),
        "OSSIE_INVALID",
        "duplicate_name",
        "/datasets/0/metrics/1/name",
    ),
    (
        "identifier-129",
        lambda m: fld(m, "note").update(name="n" * 129),
        "OSSIE_INVALID",
        "schema",
        "/datasets/0/fields/4/name",
    ),
    (
        "relationship-to-unknown",
        lambda m: rel(m).update(to="nobody"),
        "OSSIE_INVALID",
        "reference",
        "/relationships/0/to",
    ),
    (
        "column-arity",
        lambda m: rel(m).update(from_columns=["customer_id", "sale_id"]),
        "OSSIE_INVALID",
        "reference",
        "/relationships/0/to_columns",
    ),
    (
        "to-columns-cover-no-key",
        lambda m: rel(m).update(from_columns=["customer_id"], to_columns=["region"]),
        "OSSIE_INVALID",
        "reference",
        "/relationships/0/to_columns",
    ),
    (
        "primary-key-unknown",
        lambda m: ds(m).update(primary_key=["nope"]),
        "OSSIE_INVALID",
        "reference",
        "/datasets/0/primary_key/0",
    ),
    (
        "model-metric-unqualified",
        lambda m: m["metrics"][0]["expression"].update(sql="SUM(amount)"),
        "OSSIE_INVALID",
        "reference",
        "/metrics/0/expression/sql",
    ),
    (
        "metric-references-metric",
        lambda m: m["metrics"].append({"name": "twice", "expression": {"sql": "revenue * 2"}}),
        "OSSIE_INVALID",
        "reference",
        "/metrics/1/expression/sql",
    ),
    (
        "dataset-metric-qualified",
        lambda m: ds(m)["metrics"][0]["expression"].update(sql="COUNT(sales.sale_id)"),
        "OSSIE_INVALID",
        "reference",
        "/datasets/0/metrics/0/expression/sql",
    ),
    (
        "data-not-json",
        lambda m: put(ds(m), "not json"),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/custom_extensions/0/data",
    ),
    (
        "payload-fails-schema",
        lambda m: put(ds(m), {"version": 1, "element": "dataset", "colour": "red"}),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/custom_extensions/0/data",
    ),
    (
        "payload-element-mismatch",
        lambda m: put(ds(m), {"version": 1, "element": "model"}),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/custom_extensions/0/data",
    ),
    (
        "current-and-previous-vendor",
        both(
            lambda m: put(ds(m), {"version": 1, "element": "dataset"}),
            lambda m: put(ds(m), {"version": 1, "element": "dataset"}, PREVIOUS),
        ),
        "OSSIE_INVALID",
        "extension_duplicate",
        "/datasets/0/custom_extensions/1",
    ),
    (
        "entity-keys-match-no-key",
        lambda m: put(
            ds(m),
            {
                "version": 1,
                "element": "dataset",
                "entity_keys": [{"entity": "sale", "fields": ["customer_id"]}],
            },
        ),
        "OSSIE_INVALID",
        "extension_conflict",
        "/datasets/0/custom_extensions/0/data",
    ),
    (
        "one-to-one-without-key-of-from",
        lambda m: put(
            rel(m), {"version": 1, "element": "relationship", "cardinality": "one_to_one"}
        ),
        "OSSIE_INVALID",
        "extension_conflict",
        "/relationships/0/custom_extensions/0/data",
    ),
    (
        "time-zone-on-integer",
        lambda m: put(
            fld(m, "sale_id"),
            {"version": 1, "element": "field", "time_zone": {"name": "UTC"}},
        ),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/fields/0/custom_extensions/0/data",
    ),
    (
        "decimal-without-numeric",
        lambda m: put(fld(m, "amount"), {"version": 1, "element": "field", "pii": False}),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/fields/2/custom_extensions/0/data",
    ),
    (
        "calendar-on-string",
        lambda m: put(fld(m, "note"), {"version": 1, "element": "field", "calendar": "fiscal"}),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/fields/4/custom_extensions/0/data",
    ),
    (
        "payload-name-ends-in-newline",
        lambda m: put(ds(m), {"version": 1, "element": "dataset", "entity": "sale\n"}),
        "OSSIE_INVALID",
        "extension",
        "/datasets/0/custom_extensions/0/data",
    ),
    (
        "aggregate-class-disagrees",
        lambda m: put(
            m["metrics"][0], {"version": 1, "element": "metric", "aggregate_class": "holistic"}
        ),
        "OSSIE_INVALID",
        "extension",
        "/metrics/0/custom_extensions/0/data",
    ),
    (
        "payload-reference-unknown",
        lambda m: put(m["metrics"][0], {"version": 1, "element": "metric", "home_dataset": "x"}),
        "OSSIE_INVALID",
        "reference",
        "/metrics/0/custom_extensions/0/data",
    ),
    (
        "two-duckdb-dialects",
        lambda m: fld(m, "note").update(
            expression={
                "dialects": [
                    {"dialect": "DUCKDB", "expression": "note"},
                    {"dialect": "DUCKDB", "expression": "note"},
                ]
            }
        ),
        "OSSIE_INVALID",
        "schema",
        "/datasets/0/fields/4/expression/dialects/1/dialect",
    ),
]


@pytest.mark.parametrize("form", ["yaml", "json"])
@pytest.mark.parametrize(
    ("change", "code", "rule", "pointer"),
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_each_rejection_gives_its_code_rule_pointer_and_line(
    change: Change, code: str, rule: str, pointer: str, form: str
) -> None:
    model = base()
    change(model)
    content = as_yaml(model) if form == "yaml" else as_json(model)
    error = failure(content, form)
    assert error.code == code
    (problem,) = problems(error)
    assert (problem["rule"], problem["pointer"]) == (rule, pointer)
    # An unknown member is pointed at its parent (its name is document text) on its own line.
    at = (CANARY,) if CANARY in model else pointer_path(pointer)
    assert problem["line"] == str(line_of(content, at))


@pytest.mark.parametrize("form", ["yaml", "json"])
def test_the_base_model_and_every_product_element_load(form: str) -> None:
    """A synthetic model with product entries on the model, a dataset, a field, a metric and a
    relationship loads; the same under the previous vendor name."""
    for vendor in VENDORS:
        model = base()
        put(model, {"version": 1, "element": "model"}, vendor)
        put(
            ds(model),
            {
                "version": 1,
                "element": "dataset",
                "entity": "sale",
                "entity_keys": [{"entity": "sale", "fields": ["sale_id"]}],
            },
            vendor,
        )
        put(
            fld(model, "amount"),
            {"version": 1, "element": "field", "numeric": {"precision": "18", "scale": "2"}},
            vendor,
        )
        put(
            model["metrics"][0],
            {"version": 1, "element": "metric", "aggregate_class": "distributive"},
            vendor,
        )
        put(
            rel(model),
            {"version": 1, "element": "relationship", "cardinality": "many_to_one"},
            vendor,
        )
        content = as_yaml(model) if form == "yaml" else as_json(model)
        loaded = load(content, form)
        assert {e.element for e in loaded.product_extensions} == {
            "model",
            "dataset",
            "field",
            "metric",
            "relationship",
        }
        assert loaded.registry_entry == "c721ffc"
        assert not loaded.preservation_only


def test_two_yaml_documents_duplicate_keys_and_anchors_are_syntax() -> None:
    text = as_yaml(base()).decode()
    for broken in (
        text + "---\nname: other\n",
        text + "name: again\n",
        text.replace("source: sales", "source: &s sales", 1),
    ):
        error = failure(broken.encode(), "yaml")
        assert problems(error)[0]["rule"] == "syntax"


@pytest.mark.parametrize(
    "text",
    [
        '{"version": "0.2.0.dev0", "version": "0.2.0.dev0"}',
        '{"version": NaN}',
        '{"version": Infinity}',
        '{"version": -Infinity}',
        "{} {}",
        "﻿{x",
        "[" * 40 + "]" * 40,
    ],
)
def test_adversarial_json_is_syntax(text: str) -> None:
    error = failure(text.encode(), "json")
    assert [p["rule"] for p in problems(error)] == ["syntax"]


def test_an_oversized_document_is_syntax() -> None:
    content = b" " * (documents.MAX_BYTES + 1)
    for form in ("yaml", "json"):
        assert problems(failure(content, form))[0]["rule"] == "syntax"


def test_unknown_and_old_versions() -> None:
    for version in ("0.3.0", "0.1.0"):
        model = base()
        model["version"] = version
        error = failure(as_yaml(model), "yaml")
        assert error.code == "UNSUPPORTED_OSSIE_VERSION"
        assert dict(error.details) == {"found": version, "supported": "0.2.0.dev0"}


def test_foreign_entries_are_listed_and_never_interpreted() -> None:
    model = base()
    put(ds(model), "{not even json", "OTHER")
    put(rel(model), {"cardinality": "one_to_one"}, "bi-platform")
    loaded = load(as_json(model), "json")
    assert [(e.vendor_name, e.pointer) for e in loaded.foreign_extensions] == [
        ("OTHER", "/datasets/0/custom_extensions/0"),
        ("bi-platform", "/relationships/0/custom_extensions/0"),
    ]
    assert loaded.product_extensions == ()


def test_dialect_selection() -> None:
    model = base()
    fld(model, "note")["expression"] = {
        "dialects": [
            {"dialect": "SNOWFLAKE", "expression": "note"},
            {"dialect": "DUCKDB", "expression": "lower(note)"},
        ]
    }
    loaded = load(as_yaml(model), "yaml", engines={"sales": "DUCKDB"})
    chosen = {e.element: (e.dialect, e.pointer) for e in loaded.expressions}
    assert chosen["/datasets/0/fields/4"] == (
        "DUCKDB",
        "/datasets/0/fields/4/expression/dialects/1/expression",
    )
    assert chosen["/metrics/0"] == ("OSSIE_SQL_2026", "/metrics/0/expression/sql")
    fld(model, "note")["expression"] = {
        "dialects": [{"dialect": "NOT_A_DIALECT", "expression": "x"}]
    }
    for engines in ({}, {"sales": "DUCKDB"}):
        error = failure(as_yaml(model), "yaml", engines=engines)
        assert (error.code, dict(error.details)) == (
            "UNSUPPORTED_OPERATION",
            {"operation": "expression_dialect"},
        )


def test_an_expression_only_in_the_product_dialect_is_not_selected() -> None:
    """Ossie expressions cannot name metrics; an entry under the product's token is kept in
    the document, never selected by this revision's reader (Owner policy of 2026-11-01)."""
    model = base()
    model["metrics"].append(
        {
            "name": "twice",
            "expression": {"dialects": [{"dialect": CURRENT, "expression": "revenue * 2"}]},
        }
    )
    error = failure(as_yaml(model), "yaml", {"sales": "DUCKDB", "customers": "DUCKDB"})
    assert (error.code, dict(error.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "expression_dialect"},
    )


# --- temporal relationships (Owner decision of 2026-11-01, CASE-013) ---------------------------


def temporal_model() -> Model:
    model = base()
    model["datasets"][1] = {
        "name": "customers",
        "source": "customer_history",
        "primary_key": ["customer_id", "valid_from"],
        "fields": [field("customer_id"), field("valid_from"), field("valid_to")],
    }
    return model


def temporal(**more: str) -> Model:
    rule = {
        "event_time": "sales.sold_at",
        "valid_from": "customers.valid_from",
        "valid_to": "customers.valid_to",
        "boundary": "closed_open",
        "match": "exactly_one",
        **more,
    }
    return {"version": 1, "element": "relationship", "temporal": rule}


@pytest.mark.parametrize("form", ["yaml", "json"])
def test_a_declared_as_of_match_excuses_key_coverage(form: str) -> None:
    model = temporal_model()
    put(rel(model), temporal())
    put(rel(model), {"temporal": "anything"}, "bi-platform")
    loaded = load(as_yaml(model) if form == "yaml" else as_json(model), form)
    assert len(loaded.foreign_extensions) == 1


@pytest.mark.parametrize(
    ("change", "rule", "pointer"),
    [
        (lambda m: None, "reference", "/relationships/0/to_columns"),
        (lambda m: put(rel(m), temporal(), "OTHER"), "reference", "/relationships/0/to_columns"),
        (
            lambda m: put(rel(m), {"version": 1, "element": "relationship"}),
            "reference",
            "/relationships/0/to_columns",
        ),
        (
            both(
                lambda m: ds(m, "customers").update(primary_key=["customer_id", "valid_to"]),
                lambda m: put(rel(m), temporal()),
            ),
            "extension_conflict",
            "/relationships/0/custom_extensions/0/data",
        ),
        (
            both(
                lambda m: rel(m).update(from_columns=["customer_id", "sold_at"]),
                lambda m: rel(m).update(to_columns=["customer_id", "valid_to"]),
                lambda m: put(rel(m), temporal()),
            ),
            "extension_conflict",
            "/relationships/0/custom_extensions/0/data",
        ),
        (
            lambda m: put(rel(m), temporal(event_time="customers.valid_to")),
            "extension_conflict",
            "/relationships/0/custom_extensions/0/data",
        ),
        (
            both(
                lambda m: put(
                    ds(m, "customers"),
                    {
                        "version": 1,
                        "element": "dataset",
                        "time_roles": {"validity": {"from": "valid_to", "to": "valid_from"}},
                    },
                ),
                lambda m: put(rel(m), temporal()),
            ),
            "extension_conflict",
            "/relationships/0/custom_extensions/0/data",
        ),
    ],
    ids=[
        "no-entry",
        "foreign-entry-only",
        "product-entry-without-temporal",
        "valid-from-outside-every-key",
        "validity-field-in-to-columns",
        "event-time-not-of-from",
        "dataset-validity-disagrees",
    ],
)
def test_a_temporal_exception_needs_a_coherent_product_declaration(
    change: Change, rule: str, pointer: str
) -> None:
    model = temporal_model()
    change(model)
    for form, content in (("yaml", as_yaml(model)), ("json", as_json(model))):
        (problem,) = problems(failure(content, form))
        assert (problem["rule"], problem["pointer"]) == (rule, pointer)


@pytest.mark.parametrize("form", ["yaml", "json"])
def test_an_incoherent_temporal_declaration_is_refused_even_when_keys_are_covered(
    form: str,
) -> None:
    model = base()
    region = "customers.region"
    put(rel(model), temporal(event_time=region, valid_from=region, valid_to=region))
    content = as_yaml(model) if form == "yaml" else as_json(model)
    (problem,) = problems(failure(content, form))
    assert (problem["rule"], problem["pointer"]) == (
        "extension_conflict",
        "/relationships/0/custom_extensions/0/data",
    )


# --- representation, preservation and safety ----------------------------------------------


def test_yaml_and_json_give_equal_models_and_keep_their_own_bytes() -> None:
    model = base()
    put(ds(model), {"version": 1, "element": "dataset", "entity": "sale"})
    from_yaml, from_json = load(as_yaml(model), "yaml"), load(as_json(model), "json")
    assert from_yaml == from_json
    assert (from_yaml.format, from_json.format) == ("yaml", "json")
    assert from_yaml.content == as_yaml(model)
    assert from_json.identity == hashlib.sha256(as_json(model)).hexdigest()


def test_bytes_and_identity_survive_comments_foreign_entries_and_odd_formatting() -> None:
    text = as_yaml(base()).decode().replace("name: shop", "name:    shop   # a comment", 1)
    text = "# heading\n" + text + "custom_extensions:\n- {vendor_name: OTHER, data: '{}'}\n"
    content = text.encode()
    loaded = load(content, "yaml")
    assert loaded.content == content
    assert loaded.identity == hashlib.sha256(content).hexdigest()
    assert [e.pointer for e in loaded.foreign_extensions] == ["/custom_extensions/0"]


def test_numbers_stay_exact() -> None:
    model = base()
    model["ai_context"] = {"ratio": "RATIO", "big": "BIG"}
    text = as_json(model).decode().replace('"RATIO"', "1.10").replace('"BIG"', str(2**64 + 1))
    for form, content in (("json", text.encode()), ("yaml", text.encode())):
        loaded = load(content, form)
        context = cast(Mapping[str, object], loaded.document["ai_context"])
        assert isinstance(context["ratio"], Decimal)
        assert str(context["ratio"]) == "1.10"
        assert context["big"] == 2**64 + 1


def test_the_model_is_immutable_and_reading_is_deterministic() -> None:
    content = as_yaml(base())
    first, second = load(content, "yaml"), load(content, "yaml")
    assert first == second
    with pytest.raises(TypeError):
        first.document["name"] = "x"  # type: ignore[index]
    with pytest.raises(AttributeError):
        first.identity = "x"  # type: ignore[misc]
    model = base()
    model["relationships"][0]["to"] = "nobody"
    errors = [failure(as_yaml(model), "yaml").to_json(audience="builder") for _ in range(2)]
    assert errors[0] == errors[1]


CANARY_STEPS: list[tuple[str, Change, str]] = [
    ("syntax", lambda m: m.update({CANARY: [CANARY, CANARY]}), "syntax"),
    ("version", lambda m: m.update(version=CANARY + " !"), "schema"),
    ("schema", lambda m: m.update({CANARY: CANARY}), "schema"),
    (
        "names",
        lambda m: m["datasets"].extend(
            [{"name": CANARY, "source": CANARY}, {"name": CANARY, "source": CANARY}]
        ),
        "duplicate_name",
    ),
    ("long-name", lambda m: fld(m, "note").update(name=CANARY * 20), "schema"),
    ("reference", lambda m: rel(m).update(to=CANARY), "reference"),
    (
        "expression-reference",
        lambda m: m["metrics"][0]["expression"].update(sql=f"SUM({CANARY}.{CANARY})"),
        "reference",
    ),
    ("expression", lambda m: m["metrics"][0]["expression"].update(sql=CANARY + " #"), "expression"),
    ("payload-text", lambda m: put(ds(m), CANARY), "extension"),
    (
        "payload-schema",
        lambda m: put(ds(m), {"version": 1, "element": "dataset", CANARY: CANARY}),
        "extension",
    ),
    (
        "payload-reference",
        lambda m: put(m["metrics"][0], {"version": 1, "element": "metric", "home_dataset": CANARY}),
        "reference",
    ),
    (
        "dialect",
        lambda m: fld(m, "note").update(
            expression={"dialects": [{"dialect": CANARY, "expression": CANARY}]}
        ),
        "",
    ),
    ("ai-context", lambda m: m.update(ai_context={CANARY: [CANARY]}, version="0.3.0"), ""),
]


@pytest.mark.parametrize("form", ["yaml", "json"])
@pytest.mark.parametrize(
    ("change", "rule"), [row[1:] for row in CANARY_STEPS], ids=[row[0] for row in CANARY_STEPS]
)
def test_a_canary_never_reaches_a_message(change: Change, rule: str, form: str) -> None:
    """One failure per step, each carrying the canary where that step reads it. The one
    deliberate exception is `UNSUPPORTED_OSSIE_VERSION.found`, which the contract defines as the
    document's version when it is version-shaped; a version that is not is `schema`."""
    model = base()
    change(model)
    content = as_yaml(model) if form == "yaml" else as_json(model)
    if rule == "syntax":
        text = content.decode()
        content = (text + text[-40:] if form == "json" else text + f"{CANARY}: 1\n").encode()
    error = failure(content, form)
    if rule:
        assert {p["rule"] for p in problems(error)} == {rule}
    assert CANARY in content.decode()
    for audience in ("builder", "other"):
        for locale in ("es", "en"):
            assert CANARY not in error.to_json(locale, audience)


@pytest.mark.parametrize("form", ["yaml", "json"])
@pytest.mark.parametrize("where", ["name", "key", "foreign-data"])
def test_a_lone_surrogate_is_syntax_in_both_formats(where: str, form: str) -> None:
    model = base()
    if where == "name":
        model["name"] = "LONE"
    elif where == "key":
        model["ai_context"] = {"LONE": 1}
    else:
        put(ds(model), "LONE", "OTHER")
    content = (as_yaml(model) if form == "yaml" else as_json(model)).decode()
    escaped = content.replace("LONE", '"sh\\uD800op"' if form == "yaml" else "sh\\uD800op")
    assert [p["rule"] for p in problems(failure(escaped.encode(), form))] == ["syntax"]


_MUTATIONS = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=10_000),
        st.one_of(
            st.none(),
            st.integers(),
            st.text(max_size=5),
            st.booleans(),
            st.lists(st.integers(), max_size=2),
            st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
        ),
    ),
    min_size=1,
    max_size=4,
)


def _nodes(value: object, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found = [path]
    if isinstance(value, dict):
        for key, item in value.items():
            found += _nodes(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _nodes(item, (*path, index))
    return found


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(mutations=_MUTATIONS, form=st.sampled_from(["yaml", "json"]))
def test_mutations_give_a_model_or_a_typed_error(
    mutations: list[tuple[int, object]], form: str
) -> None:
    model = base()
    put(ds(model), {"version": 1, "element": "dataset", "entity": "sale"})
    for at, value in mutations:
        nodes = [path for path in _nodes(model) if path]
        path = nodes[at % len(nodes)]
        parent = model
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = value
    content = as_yaml(model) if form == "yaml" else as_json(model)
    code = None
    try:
        load(content, form)
    except SemanticError as error:
        code = error.code
    assert code in (None, "OSSIE_INVALID", "UNSUPPORTED_OSSIE_VERSION", "UNSUPPORTED_OPERATION")
