"""`security.yaml` and the policy model (P2-08, #304): strict reading, the closed predicate
grammar, the managed lake only, registrations, inheritance against inference, propagation
across every relationship, and non-executable policies that fail closed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from orvex.semantic import SemanticError
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.policies import PolicyModel, read_policies

from tests.support.policies import GROUPS, MANAGED_TABLES, bank

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "contracts/security-policies/v1/examples"
OWN_ZONE = "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)"


def text(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False)


def policies(**more: Any) -> dict[str, Any]:  # noqa: ANN401
    found: dict[str, Any] = {
        "version": 1,
        "row_policies": [
            {
                "name": "own_zone_only",
                "dataset": "customers",
                "kind": "predicate",
                "predicate": OWN_ZONE,
            }
        ],
        "column_policies": [
            {
                "member": "customers.national_id",
                "default": "hidden",
                "visible_to_groups": ["collections"],
            },
            {"member": "customers.phone", "default": "masked", "mask": "***"},
        ],
    }
    found.update(more)
    return found


def model(document: dict[str, Any] | str) -> PolicyModel:
    source = document if isinstance(document, str) else text(document)
    return read_policies(source, bank(), groups=GROUPS, managed_tables=MANAGED_TABLES)


def failure(document: dict[str, Any] | str) -> SemanticError:
    with pytest.raises(SemanticError) as caught:
        model(document)
    return caught.value


def rules(document: dict[str, Any] | str) -> set[str]:
    error = failure(document)
    assert error.code == "POLICY_INVALID", error.code
    problems: list[dict[str, str]] = list(error.details["problems"])  # type: ignore[call-overload]
    return {problem["rule"] for problem in problems}


def row(predicate: str, **more: Any) -> dict[str, Any]:  # noqa: ANN401
    return policies(
        row_policies=[
            {
                "name": "p",
                "dataset": "customers",
                "kind": "predicate",
                "predicate": predicate,
                **more,
            }
        ]
    )


def M(dataset: str, name: str) -> MemberId:
    return MemberId("field", dataset, name)


def METRIC(name: str) -> MemberId:
    return MemberId("metric", None, name)


# --- the contract and the §3.3 example ------------------------------------------------------


def test_the_security_md_example_is_valid() -> None:
    found = model(policies())
    phone = found.column_policies[M("CUSTOMERS", "PHONE")]
    assert (phone.default, phone.mask) == ("masked", "***")
    assert found.protection(M("CUSTOMERS", "NATIONAL_ID")) == "hidden"
    assert found.protection(M("CUSTOMERS", "NATIONAL_ID"), {"collections"}) == "visible"
    assert found.protection(M("CUSTOMERS", "SEGMENT")) == "visible"


def test_the_package_reads_the_contracts_own_schema() -> None:
    packaged = ROOT / "packages/semantic/src/orvex/semantic/policies/security-policies-v1.json"
    assert (
        packaged.read_bytes() == (ROOT / "contracts/security-policies/v1/schema.json").read_bytes()
    )


@pytest.mark.parametrize("path", sorted((EXAMPLES / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_the_contracts_valid_examples_read(path: Path) -> None:
    model(json.loads(path.read_text("utf-8")))


# --- strict YAML ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "version: 1\nversion: 1\n",
        "version: &v 1\nlimits: {policy_values: *v}\n",
        "version: !!int 1\n",
        "version: 1\n1: x\n",
        "version: 1\n---\nversion: 1\n",
        "version: [1\n",
        "- 1\n" + "  " * 40 + "- 2\n",
    ],
)
def test_yaml_that_is_not_strict_is_refused(source: str) -> None:
    assert rules(source) <= {"syntax", "schema"}
    assert rules(source)


def test_yaml_one_point_one_surprises_stay_text() -> None:
    # `yes` is a string here, so it is a schema error, never a boolean that passes.
    assert rules("version: 1\nlimits: {policy_values: 010}\n") == {"schema"}


def test_a_large_file_is_refused() -> None:
    assert rules("version: 1\n# " + "x" * (1024 * 1024) + "\n") == {"syntax"}


# --- the predicate grammar ------------------------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [
        "zone_id = $zone_id",
        "zone_id IN ($zones)",
        "segment <> 'vip' AND zone_id IS NOT NULL",
        "NOT (segment = 'vip') OR zone_id = 7",
        OWN_ZONE,
        "EXISTS (SELECT 1 FROM security.user_zone"
        " WHERE user_zone.zone_id = customers.zone_id AND user_id = $user_id)",
    ],
)
def test_each_allowed_shape_parses(predicate: str) -> None:
    model(row(predicate))


@pytest.mark.parametrize(
    "predicate",
    [
        "upper(segment) = 'VIP'",  # a function call
        "segment = 'a' || 'b'",  # concatenation
        "zone_id = 1; DROP TABLE x",  # a statement separator
        "zone_id = 1 -- comment",  # a comment
        "zone_id IN (SELECT zone_id FROM security.user_zone, security.registered)",  # two tables
        "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id > $u)",  # non-equality
        "segment = '$user_id'",  # an attribute inside a string
        "zone_id + 1 = 2",  # arithmetic
    ],
)
def test_each_refused_shape_gives_a_predicate_problem(predicate: str) -> None:
    assert rules(row(predicate)) == {"predicate"}


def test_an_unknown_field_or_column_is_a_reference_problem() -> None:
    assert rules(row("missing = $x")) == {"reference"}
    assert rules(row("zone_id IN (SELECT nothing FROM security.user_zone WHERE user_id = $u)")) == {
        "reference"
    }


def test_no_problem_ever_carries_the_predicate() -> None:
    error = failure(row("upper(segment) = 'secret-c4n4ry'"))
    for audience in ("builder", "other"):
        assert "c4n4ry" not in error.to_json("en", audience)


# --- the managed lake only, and registrations ---------------------------------------------


def test_a_subquery_outside_the_managed_lake_is_not_supported() -> None:
    error = failure(row("zone_id IN (SELECT zone_id FROM external.users WHERE user_id = $user_id)"))
    assert error.code == "POLICY_NOT_SUPPORTED_ON_ENGINE"
    assert error.details["reason"] == "subquery_outside_lake"
    assert "external" not in error.to_json("en", "other")


@pytest.mark.parametrize(
    "predicate",
    [
        "zone_id NOT IN (SELECT zone_id FROM security.blocked_zone WHERE user_id = $user_id)",
        "NOT EXISTS (SELECT 1 FROM security.blocked_zone"
        " WHERE blocked_zone.zone_id = customers.zone_id AND user_id = $user_id)",
        "NOT (zone_id IN (SELECT zone_id FROM security.blocked_zone WHERE user_id = $user_id))",
    ],
)
def test_a_deny_list_needs_a_registration(predicate: str) -> None:
    assert rules(row(predicate)) == {"registration"}
    registered = model(
        row(predicate, registration={"table": "security.registered", "key": "user_id"})
    )
    assert registered.row_policies[0].deny_list
    assert dict(registered.row_policies[0].registration or {}) == {
        "table": "security.registered",
        "key": "user_id",
    }


def test_an_allow_list_needs_no_registration() -> None:
    assert not model(row(OWN_ZONE)).row_policies[0].deny_list


# --- references and duplicates -------------------------------------------------------------


def test_unknown_references_are_refused() -> None:
    assert rules(
        policies(
            row_policies=[
                {"name": "p", "dataset": "nowhere", "kind": "predicate", "predicate": "x = 1"}
            ]
        )
    ) == {"reference"}
    assert rules(row(OWN_ZONE, exempt_groups=["strangers"])) == {"reference"}
    assert rules(row(OWN_ZONE, roles=["no_such_relationship"])) == {"reference"}
    assert rules(
        policies(column_policies=[{"member": "customers.nothing", "default": "hidden"}])
    ) == {"reference"}


def test_a_hidden_member_is_never_named_in_another_audiences_error() -> None:
    error = failure(
        policies(
            column_policies=[
                {"member": "customers.national_id", "default": "hidden"},
                {"member": "customers.national_id", "default": "masked", "mask": "*"},
            ]
        )
    )
    assert error.code == "DUPLICATE_COLUMN_POLICY"
    assert "national_id" not in error.to_json("en", "other")
    assert "national_id" in error.to_json("en", "builder")


def test_two_row_policies_with_one_name_are_refused() -> None:
    twice = {"name": "same", "dataset": "customers", "kind": "predicate", "predicate": OWN_ZONE}
    assert rules(policies(row_policies=[dict(twice), dict(twice)])) == {"duplicate_name"}


def test_a_metric_is_never_masked() -> None:
    assert rules(
        policies(column_policies=[{"member": "revenue", "default": "masked", "mask": "*"}])
    ) == {"masked_metric"}


# --- inheritance, against inference ---------------------------------------------------------


def test_derived_members_inherit_the_strictest_protection() -> None:
    found = model(policies())
    assert found.protection(METRIC("IDENTIFIED")) == "hidden"  # COUNT(DISTINCT national_id)
    assert found.protection(METRIC("FLAGGED")) == "hidden"  # a filter reading national_id
    assert found.protection(METRIC("PHONES")) == "hidden"  # a metric over a masked field
    assert found.protection(M("CUSTOMERS", "PHONE_TAIL")) == "masked"  # a calculated field
    assert found.protection(METRIC("TAILS")) == "hidden"  # a chain of two derivations
    assert found.protection(METRIC("RUNNING")) == "hidden"  # a window partitioned by it
    assert found.protection(METRIC("REVENUE")) == "visible"
    assert (
        found.protection(METRIC("IDENTIFIED"), {"collections"}) == "visible"
    )  # their group sees it
    assert M("CUSTOMERS", "NATIONAL_ID") in found.inherited(METRIC("IDENTIFIED"))


def test_a_hidden_relationship_key_stays_hidden_through_the_related_dataset() -> None:
    found = model(
        policies(column_policies=[{"member": "customers.customer_id", "default": "hidden"}])
    )
    assert found.protection(M("PAYMENTS", "CUSTOMER_ID")) == "hidden"
    assert found.protection(M("PROFILES", "CUSTOMER_ID")) == "hidden"


# --- propagation across every relationship ----------------------------------------------------


def reached(found: PolicyModel, name: str) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    by: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for reach in found.propagation[name]:
        by.setdefault(reach.dataset, []).append((reach.path, reach.via))
    return by


def test_propagation_reaches_every_dataset_by_every_path() -> None:
    zones = {
        "name": "zones_policy",
        "dataset": "zones",
        "kind": "predicate",
        "predicate": "zone_id = $zone_id",
    }
    found = reached(model(policies(row_policies=[zones])), "zones_policy")
    assert set(found) == {"CUSTOMERS", "PAYMENTS", "PROFILES", "ALLOCATIONS"}
    assert sorted(found["PAYMENTS"]) == [
        (("CUSTOMER_ZONE", "BILLING"), "many_to_one"),
        (("CUSTOMER_ZONE", "SHIPPING"), "many_to_one"),
    ]
    assert found["PROFILES"] == [(("CUSTOMER_ZONE", "PROFILE"), "one_to_one")]
    assert found["ALLOCATIONS"][0][1] == "bridge_allocation"


def test_one_to_one_propagates_both_ways_and_a_fact_reaches_no_dimension() -> None:
    profiles = {
        "name": "profiles_policy",
        "dataset": "profiles",
        "kind": "predicate",
        "predicate": "score > 1",
    }
    payments = {
        "name": "payments_policy",
        "dataset": "payments",
        "kind": "predicate",
        "predicate": "amount > 1",
    }
    found = model(policies(row_policies=[profiles, payments]))
    assert "CUSTOMERS" in reached(found, "profiles_policy")
    assert set(reached(found, "payments_policy")) == {"ALLOCATIONS"}


def test_named_roles_restrict_role_paths_only() -> None:
    zones = {
        "name": "z",
        "dataset": "zones",
        "kind": "predicate",
        "predicate": "zone_id = $zone_id",
        "roles": ["billing"],
    }
    found = reached(model(policies(row_policies=[zones])), "z")
    assert [path for path, _ in found["PAYMENTS"]] == [("CUSTOMER_ZONE", "BILLING")]


def test_a_self_relationship_with_a_cycle_ends() -> None:
    staff = {
        "name": "staff",
        "dataset": "employees",
        "kind": "predicate",
        "predicate": "employee_id = $employee",
    }
    assert reached(model(policies(row_policies=[staff])), "staff") == {}


# --- non-executable policies fail closed ------------------------------------------------------


def test_a_hierarchy_policy_marks_its_dataset_and_what_it_reaches_unusable() -> None:
    hierarchy = {
        "name": "org", "dataset": "customers", "kind": "hierarchy",
        "closure": {
            "table": "security.org_closure", "ancestor": "ancestor", "descendant": "descendant",
        },
        "node": "org_node", "field": "zone_id",
    }  # fmt: skip
    found = model(policies(row_policies=[hierarchy]))
    for dataset in ("CUSTOMERS", "PAYMENTS", "PROFILES"):
        with pytest.raises(SemanticError) as caught:
            found.check_usable([dataset])
        assert caught.value.details == {"operation": "hierarchy_policy"}
    found.check_usable(["ZONES"])


def test_a_cell_policy_and_a_threshold_fail_closed() -> None:
    cell = {
        "member": "customers.segment",
        "default": "visible",
        "mask": "***",
        "visible_when": "zone_id = $zone_id",
    }
    threshold = {"dataset": "payments", "entity": "payments.customer_id", "min_group_size": 5}
    found = model(policies(column_policies=[cell], aggregate_thresholds=[threshold]))
    assert found.protection(M("CUSTOMERS", "SEGMENT"), GROUPS) == "masked"  # never listed visible
    for dataset, operation in (("CUSTOMERS", "cell_policy"), ("PAYMENTS", "min_group_size")):
        with pytest.raises(SemanticError) as caught:
            found.check_usable([dataset])
        assert caught.value.details == {"operation": operation}


# --- no implicit exemption ----------------------------------------------------------------------


def test_nobody_is_exempt_unless_a_policy_names_the_group() -> None:
    found = model(policies())
    assert found.row_policies[0].exempt_groups == frozenset()
    exempt = model(row(OWN_ZONE, exempt_groups=["management"]))
    assert exempt.row_policies[0].exempt_groups == {"management"}


# --- determinism ------------------------------------------------------------------------------


def test_the_fingerprint_ignores_order_and_follows_meaning() -> None:
    base = policies()
    reordered = policies(column_policies=list(reversed(base["column_policies"])))
    assert model(base).fingerprint == model(reordered).fingerprint
    assert (
        model(base).fingerprint
        != model(
            row(
                OWN_ZONE.replace("zone_id IN", "zone_id NOT IN"),
                registration={"table": "security.registered", "key": "user_id"},
            )
        ).fingerprint
    )


def test_the_policy_model_is_immutable() -> None:
    found = model(policies())
    with pytest.raises(TypeError):
        found.column_policies[M("CUSTOMERS", "SEGMENT")] = None  # type: ignore[index]
    assert json.dumps(sorted(found.unusable)) == "[]"
