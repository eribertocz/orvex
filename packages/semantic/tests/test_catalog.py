"""The catalog (P2-10, #309): what one caller may query, and Ossie export with a loss report."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from orvex.semantic.catalog import ExportRefused, catalog_for, export_model
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies

from tests.support.policies import CATALOG, GROUPS, MANAGED_TABLES, MODEL, VENDORS, extension

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "contracts/catalog/v1/examples"
Document = dict[str, Any]


def compiled_from(document: Document) -> CompiledModel:
    return compile_model(
        read(json.dumps(document).encode(), "semantic/bank.ossie.json", VENDORS), CATALOG, VENDORS
    )


def described() -> Document:
    document = copy.deepcopy(MODEL)
    customers = next(d for d in document["datasets"] if d["name"] == "customers")
    customers["description"] = "Every customer we bill."
    for node in customers["fields"]:
        if node["name"] == "segment":
            node["description"] = "Commercial segment, derived from national_id for companies."
        if node["name"] == "zone_id":
            node["description"] = "The zone the customer belongs to."
        if node["name"] == "phone":
            node["description"] = "phone-canary-description"
            node["custom_extensions"] = [
                extension({"element": "field", "semantic_type": "category"})
            ]
    return document


def without(document: Document, dataset: str, field: str) -> Document:
    found = copy.deepcopy(document)
    target = next(d for d in found["datasets"] if d["name"] == dataset)
    target["fields"] = [f for f in target["fields"] if f["name"] != field]
    return found


def policies_for(compiled: CompiledModel, **more: Any) -> PolicyModel:  # noqa: ANN401
    document: Document = {
        "version": 1,
        "column_policies": [
            {
                "member": "customers.national_id",
                "default": "hidden",
                "visible_to_groups": ["collections"],
            },
            {"member": "customers.phone", "default": "masked", "mask": "***"},
        ],
    }
    document.update(more)
    return read_policies(
        yaml.safe_dump(document), compiled, groups=GROUPS, managed_tables=MANAGED_TABLES
    )


def catalog(
    document: Document | None = None,
    groups: frozenset[str] = frozenset(),
    builder: bool = False,
    **more: Any,  # noqa: ANN401
) -> Document:
    compiled = compiled_from(document or described())
    return catalog_for(compiled, policies_for(compiled, **more), groups=groups, builder=builder)


def names(found: Document, dataset: str) -> list[str]:
    entry = next(d for d in found["datasets"] if d["name"] == dataset)
    return [f["name"] for f in entry["fields"]] + [m["name"] for m in entry["metrics"]]


UNIT_CANARY = "[ppth]"


def field_of(found: Document, dataset: str, name: str) -> Document:
    entry = next(d for d in found["datasets"] if d["name"] == dataset)
    field: Document = next(f for f in entry["fields"] if f["name"] == name)
    return field


def without_identity(found: Document) -> Document:
    return {k: v for k, v in found.items() if k != "model_fingerprint"}


# --- the contract ---------------------------------------------------------------------------


def test_the_package_reads_the_contracts_own_schema() -> None:
    packaged = ROOT / "packages/semantic/src/orvex/semantic/catalog/catalog-v1.json"
    assert packaged.read_bytes() == (ROOT / "contracts/catalog/v1/schema.json").read_bytes()


# --- hidden is absent, indistinguishable from missing ---------------------------------------


def test_a_hidden_field_is_absent_byte_for_byte() -> None:
    """The catalog equals, byte for byte, the catalog of the model without the field and the
    metrics that read it, its model fingerprint aside (that identifies another model)."""
    hidden = catalog(copy.deepcopy(MODEL))
    removed = without(MODEL, "customers", "national_id")
    removed["metrics"] = [
        m for m in removed["metrics"] if m["name"] not in {"identified", "flagged", "running"}
    ]
    assert "national_id" not in json.dumps(hidden).lower()
    phone_only = [{"member": "customers.phone", "default": "masked", "mask": "***"}]
    reference = catalog(removed, column_policies=phone_only)
    assert json.dumps(without_identity(hidden)) == json.dumps(without_identity(reference))


def test_a_metric_that_inherits_hidden_is_absent() -> None:
    found = catalog()
    metrics = [m["name"] for m in found["metrics"]]
    assert "identified" not in metrics
    assert "flagged" not in metrics
    assert "revenue" in metrics
    assert "identified" in [
        m["name"] for m in catalog(groups=frozenset({"collections"}))["metrics"]
    ]


def test_a_dataset_whose_members_are_all_hidden_is_absent() -> None:
    hide = [{"member": f"zones.{name}", "default": "hidden"} for name in ("zone_id", "label")]
    found = catalog(column_policies=hide)
    assert "zones" not in [d["name"] for d in found["datasets"]]
    assert "customer_zone" not in [r["name"] for r in found["relationships"]]


# --- masked shows nothing of itself -----------------------------------------------------------


def test_a_masked_field_shows_only_that_it_is_masked_text() -> None:
    found = catalog()
    customers = next(d for d in found["datasets"] if d["name"] == "customers")
    phone = next(f for f in customers["fields"] if f["name"] == "phone")
    assert phone == {"name": "phone", "masked": True, "encoding": "VARCHAR"}
    for canary in ("phone-canary-description", "category", "String"):
        assert canary not in json.dumps(phone), canary
    assert "phone-canary-description" not in json.dumps(found)
    assert '"category"' not in json.dumps(found)
    tail = next(f for f in customers["fields"] if f["name"] == "phone_tail")
    assert tail == {"name": "phone_tail", "masked": True, "encoding": "VARCHAR"}  # inherited


# --- relationships ---------------------------------------------------------------------------


def test_a_relationship_with_a_protected_key_is_absent() -> None:
    found = catalog(column_policies=[{"member": "customers.customer_id", "default": "hidden"}])
    assert {"billing", "shipping", "profile"}.isdisjoint(r["name"] for r in found["relationships"])
    assert "customer_zone" in [r["name"] for r in found["relationships"]]
    assert "customer_id" not in names(found, "payments")  # paired key inherits hidden


# --- unusable datasets ------------------------------------------------------------------------


def hierarchy() -> list[Document]:
    return [
        {
            "name": "org", "dataset": "employees", "kind": "hierarchy",
            "closure": {
                "table": "security.org_closure", "ancestor": "ancestor", "descendant": "descendant",
            },
            "node": "org_node", "field": "employee_id",
        }
    ]  # fmt: skip


def test_an_unusable_dataset_is_listed_by_name_only_when_its_existence_is_authorized() -> None:
    found = catalog(row_policies=hierarchy())
    entry = next(d for d in found["datasets"] if d["name"] == "employees")
    assert entry == {"name": "employees", "available": False, "reason": "not_available_yet"}
    hidden_all = [
        {"member": f"employees.{n}", "default": "hidden"} for n in ("employee_id", "manager_id")
    ]
    unseen = catalog(row_policies=hierarchy(), column_policies=hidden_all)
    reference = catalog(column_policies=hidden_all)
    assert without_identity(unseen) == without_identity(reference)
    assert "employees" not in json.dumps(unseen)


def test_a_threshold_or_cell_policy_marks_its_dataset_not_available() -> None:
    threshold = [{"dataset": "payments", "entity": "payments.customer_id", "min_group_size": 5}]
    found = catalog(aggregate_thresholds=threshold)
    entry = next(d for d in found["datasets"] if d["name"] == "payments")
    assert entry["available"] is False
    assert "revenue" not in [m["name"] for m in found["metrics"]]  # its lineage is unusable


# --- descriptions -----------------------------------------------------------------------------


def test_a_flagged_description_reaches_builders_only() -> None:
    viewer = catalog()
    builder = catalog(builder=True)
    assert "description" not in field_of(viewer, "customers", "segment")
    assert field_of(builder, "customers", "segment")["description_flagged"] is True
    zone_viewer = field_of(viewer, "customers", "zone_id")
    assert zone_viewer["description"] == field_of(builder, "customers", "zone_id")["description"]
    assert "national_id" not in json.dumps(viewer)


# --- permissions, not roles -----------------------------------------------------------------


def test_builder_permission_widens_no_member() -> None:
    viewer, builder = catalog(), catalog(builder=True)

    def strip(found: Document) -> Document:
        stripped: Document = json.loads(
            json.dumps(found).replace(', "description_flagged": true', "")
        )
        return stripped

    assert [names(viewer, d["name"]) for d in viewer["datasets"] if d.get("available")] == [
        names(builder, d["name"]) for d in builder["datasets"] if d.get("available")
    ]
    assert strip(builder)["metrics"] == viewer["metrics"]


def test_a_group_in_visible_to_groups_shows_the_member() -> None:
    assert "national_id" in names(catalog(groups=frozenset({"collections"})), "customers")


def test_equal_visibility_gives_equal_catalog_fingerprints() -> None:
    assert (
        catalog(groups=frozenset({"analysts"}))["catalog_fingerprint"]
        == catalog()["catalog_fingerprint"]
    )
    assert (
        catalog(groups=frozenset({"collections"}))["catalog_fingerprint"]
        != catalog()["catalog_fingerprint"]
    )


def test_the_catalog_validates_against_its_contract() -> None:
    from orvex.semantic.catalog.building import schema_validator

    for found in (catalog(), catalog(builder=True), catalog(row_policies=hierarchy())):
        assert not list(schema_validator().iter_errors(found))


@pytest.mark.parametrize("path", sorted((EXAMPLES / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_valid_examples_validate(path: Path) -> None:
    from orvex.semantic.catalog.building import schema_validator

    assert not list(schema_validator().iter_errors(json.loads(path.read_text("utf-8"))))


# --- export ---------------------------------------------------------------------------------


def test_export_with_extensions_is_lossless() -> None:
    compiled = compiled_from(described())
    found = export_model(
        compiled, role="builder", target_entry=compiled.ossie.registry_entry, mode="with_extensions"
    )
    assert found.losses == ()
    again = compile_model(
        read(found.content, "semantic/bank.ossie.json", VENDORS), CATALOG, VENDORS
    )
    assert again.model_fingerprint == compiled.model_fingerprint


def test_export_standard_only_reports_every_loss() -> None:
    compiled = compiled_from(described())
    found = export_model(
        compiled, role="builder", target_entry=compiled.ossie.registry_entry, mode="standard_only"
    )
    assert len(found.losses) == len(compiled.ossie.product_extensions)
    by_pointer = {loss.pointer: loss for loss in found.losses}
    profile = next(loss for loss in found.losses if "cardinality" in loss.declarations)
    assert "fan out" in profile.consequences[profile.declarations.index("cardinality")]
    assert any("aggregate_class" in loss.declarations for loss in by_pointer.values())
    reread = read(found.content, "semantic/bank.ossie.json", VENDORS)
    assert reread.product_extensions == ()
    assert b"security" not in found.content.lower()


def test_export_is_for_builders_and_one_entry() -> None:
    compiled = compiled_from(described())
    for role in ("viewer", "author", "admin", ""):
        with pytest.raises(ExportRefused):
            export_model(
                compiled,
                role=role,
                target_entry=compiled.ossie.registry_entry,
                mode="with_extensions",
            )
    with pytest.raises(ExportRefused):
        export_model(compiled, role="builder", target_entry="another-entry", mode="with_extensions")
    mode: Any = "everything"
    with pytest.raises(ExportRefused):
        export_model(
            compiled, role="builder", target_entry=compiled.ossie.registry_entry, mode=mode
        )


def test_export_is_deterministic() -> None:
    compiled = compiled_from(described())
    entry = compiled.ossie.registry_entry
    one = export_model(compiled, role="builder", target_entry=entry, mode="standard_only")
    two = export_model(
        compiled_from(described()), role="builder", target_entry=entry, mode="standard_only"
    )
    assert one == two


# --- the caller's context --------------------------------------------------------------------


def context_document(**change: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "security_context",
        "version": 1,
        "user": "u-7",
        "project": "p1",
        "role": "viewer",
        "groups": ["collections"],
        "attributes": {},
        "permission_fingerprint": "sha256:" + "c" * 64,
    }
    found.update(change)
    return found


def test_the_catalog_follows_a_context_document() -> None:
    from orvex.semantic.catalog import catalog_for_context

    compiled = compiled_from(described())
    policies = policies_for(compiled)
    found = catalog_for_context(compiled, policies, context_document())
    assert "national_id" in names(found, "customers")
    admin_like = catalog_for_context(compiled, policies, context_document(role="author", groups=[]))
    assert "national_id" not in names(admin_like, "customers")


@pytest.mark.parametrize(
    "change",
    [
        {"groups": ["b", "a"]},
        {"is_admin": True},
        {"role": "owner"},
        {"permission_fingerprint": "sha256:XYZ"},
        {"user": "ana@example.com"},
    ],
)
def test_a_malformed_context_is_refused(change: Document) -> None:
    from orvex.semantic.catalog import InvalidCatalog, catalog_for_context

    compiled = compiled_from(described())
    with pytest.raises(InvalidCatalog):
        catalog_for_context(compiled, policies_for(compiled), context_document(**change))


def test_a_differently_quoted_spelling_of_a_hidden_member_stays_hidden() -> None:
    found = catalog(column_policies=[{"member": 'customers."NATIONAL_ID"', "default": "hidden"}])
    assert "national_id" not in names(found, "customers")


# --- the Reviewer's first pass: each finding covered for its class ----------------------------

CORPUS = ROOT / "conformance/semantic-corpus/bi-platform/models"


def test_a_hidden_grain_dimension_is_never_named() -> None:
    document = described()
    customers = next(d for d in document["datasets"] if d["name"] == "customers")
    customers["custom_extensions"] = [
        extension({"element": "dataset", "grain": {"dimensions": ["customer_id", "national_id"]}})
    ]
    viewer = catalog(document)
    assert "national_id" not in json.dumps(viewer).lower()
    entry = next(d for d in viewer["datasets"] if d["name"] == "customers")
    assert "grain" not in entry
    collections = catalog(document, groups=frozenset({"collections"}))
    entry = next(d for d in collections["datasets"] if d["name"] == "customers")
    assert entry["grain"] == ["customer_id", "national_id"]


@pytest.mark.parametrize("named", ["identified", "running", "phone_tail", "customers.phone_tail"])
def test_a_description_naming_an_inherited_protected_member_is_omitted(named: str) -> None:
    document = described()
    customers = next(d for d in document["datasets"] if d["name"] == "customers")
    zone = next(f for f in customers["fields"] if f["name"] == "zone_id")
    zone["description"] = f"Use with {named} when needed."
    viewer = catalog(document)
    assert "description" not in field_of(viewer, "customers", "zone_id")
    assert f"Use with {named}" not in json.dumps(viewer)
    assert field_of(catalog(document, builder=True), "customers", "zone_id")["description_flagged"]


NOT_YET = {"CASE-021"}  # compiles after P2-20 (#283)


def corpus_case(case: str) -> tuple[CompiledModel, Any]:
    from orvex.semantic.compiler.catalog import SourceColumn

    from conformance.runners import bi_platform_adapter as adapter

    path = CORPUS / f"{case}.yaml"
    translated, _ = adapter.translate(yaml.safe_load(path.read_text("utf-8")), VENDORS[0])
    written = yaml.safe_load(path.with_name(f"{case}.catalog.yaml").read_text("utf-8"))
    source_catalog = {
        source: {column: SourceColumn(*shape) for column, shape in columns.items()}
        for source, columns in adapter.translate_catalog(written).items()
    }
    loaded = read(json.dumps(translated).encode(), f"semantic/{case}.ossie.json", VENDORS)
    return compile_model(loaded, source_catalog, VENDORS), source_catalog


@pytest.mark.parametrize(
    "case",
    sorted(
        f"CASE-{n:03d}"
        for n in range(1, 22)
        if f"CASE-{n:03d}" not in NOT_YET | {"CASE-017"}  # refused: USD + BOB (P2-16)
    ),
)
def test_export_over_every_compiling_corpus_model(case: str) -> None:
    compiled, source_catalog = corpus_case(case)
    entry = compiled.ossie.registry_entry
    lossless = export_model(compiled, role="builder", target_entry=entry, mode="with_extensions")
    again = compile_model(
        read(lossless.content, compiled.ossie.path, VENDORS), source_catalog, VENDORS
    )
    assert again.model_fingerprint == compiled.model_fingerprint
    standard = export_model(compiled, role="builder", target_entry=entry, mode="standard_only")
    assert len(standard.losses) == len(compiled.ossie.product_extensions)
    assert read(standard.content, compiled.ossie.path, VENDORS).product_extensions == ()


def test_a_model_without_policies_lists_everything() -> None:
    compiled = compiled_from(described())
    policies = read_policies("version: 1\n", compiled, groups=GROUPS, managed_tables=MANAGED_TABLES)
    found = catalog_for(compiled, policies, groups=(), builder=False)
    listed = {
        (d["name"], m["name"]) for d in found["datasets"] for m in [*d["fields"], *d["metrics"]]
    }
    expected = {
        (d["name"], node["name"])
        for d in described()["datasets"]
        for node in [*d["fields"], *d.get("metrics", [])]
    }
    assert listed == expected
    assert len(found["metrics"]) == len(described()["metrics"])
    assert len(found["relationships"]) == len(described()["relationships"])


def visible_when() -> list[Document]:
    return [
        {
            "member": "customers.segment",
            "default": "visible",
            "mask": "*",
            "visible_when": "zone_id = $z",
        }
    ]


def test_a_cell_policy_dataset_is_listed_by_name_only() -> None:
    # A cell policy needs a visible or masked default, so its member is always seen and the
    # dataset is listed by name only; the absent case is covered with a hierarchy policy above.
    hidden_all = [
        {"member": f"customers.{n}", "default": "hidden"}
        for n in ("customer_id", "zone_id", "national_id", "phone", "phone_tail")
    ]
    for policies in (visible_when(), [*visible_when(), *hidden_all]):
        found = catalog(column_policies=policies)
        entry = next(d for d in found["datasets"] if d["name"] == "customers")
        assert entry == {"name": "customers", "available": False, "reason": "not_available_yet"}
        assert "zone_id = $z" not in json.dumps(found)


def test_an_author_and_a_viewer_with_the_same_groups_see_the_same_catalog() -> None:
    from orvex.semantic.catalog import catalog_for_context

    compiled = compiled_from(described())
    policies = policies_for(compiled)
    for groups in ([], ["analysts"], ["collections"]):
        author = catalog_for_context(
            compiled, policies, context_document(role="author", groups=groups)
        )
        viewer = catalog_for_context(
            compiled, policies, context_document(role="viewer", groups=groups)
        )
        assert author == viewer


def test_a_masked_numeric_field_reveals_neither_its_unit_nor_its_type() -> None:
    document = described()
    profiles = next(d for d in document["datasets"] if d["name"] == "profiles")
    score = next(f for f in profiles["fields"] if f["name"] == "score")
    score["custom_extensions"] = [extension({"element": "field", "unit": UNIT_CANARY})]
    masked = [{"member": "profiles.score", "default": "masked", "mask": "*"}]
    found = catalog(document, column_policies=masked)
    assert field_of(found, "profiles", "score") == {
        "name": "score",
        "masked": True,
        "encoding": "VARCHAR",
    }
    assert UNIT_CANARY not in json.dumps(found)
    assert '"Integer"' not in json.dumps(field_of(found, "profiles", "score"))
    shown = catalog(document, column_policies=[])
    assert field_of(shown, "profiles", "score")["unit"] == UNIT_CANARY
