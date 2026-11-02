"""A synthetic bank model and managed-lake tables for the P2-08 policy tests. No real data."""

from __future__ import annotations

import json
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.ossie.reader import read

ROOT = Path(__file__).resolve().parents[2]
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
CURRENT: str = _PRODUCT["ossie_vendor_name"]
VENDORS = (CURRENT, *_PRODUCT["previous_vendor_names"])


def extension(payload: dict[str, Any]) -> dict[str, Any]:
    return {"vendor_name": CURRENT, "data": json.dumps({"version": 1, **payload})}


def field(name: str, **payload: Any) -> dict[str, Any]:  # noqa: ANN401
    node: dict[str, Any] = {"name": name, "expression": {"sql": name}}
    if payload:
        node["custom_extensions"] = [extension({"element": "field", **payload})]
    return node


def ds(name: str, key: str, cols: list[Any], **more: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "name": name,
        "source": name,
        "primary_key": [key],
        "fields": [field(c) if isinstance(c, str) else c for c in cols],
        **more,
    }


def rel(name: str, frm: str, to: str, fc: str, tc: str, card: str | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {
        "name": name,
        "from": frm,
        "to": to,
        "from_columns": [fc],
        "to_columns": [tc],
    }
    if card:
        r["custom_extensions"] = [extension({"element": "relationship", "cardinality": card})]
    return r


calc = field("phone_tail", calculated={"expression": "RIGHT(phone, 4)", "max_cardinality": "10000"})
calc["expression"] = {"sql": "phone"}
MODEL: dict[str, Any] = {
    "version": "0.2.0.dev0",
    "name": "bank",
    "datasets": [
        ds("zones", "zone_id", ["zone_id", "label"]),
        ds(
            "customers",
            "customer_id",
            ["customer_id", "zone_id", "national_id", "phone", "segment", calc],
        ),
        ds("profiles", "customer_id", ["customer_id", "score"]),
        ds(
            "payments",
            "payment_id",
            ["payment_id", "customer_id", "shipping_id", "amount", "paid_on"],
        ),
        ds("employees", "employee_id", ["employee_id", "manager_id"]),
        ds("campaigns", "campaign_id", ["campaign_id", "title"]),
        ds(
            "allocations",
            "allocation_id",
            ["allocation_id", "payment_id", "campaign_id", "weight"],
            custom_extensions=[
                extension(
                    {
                        "element": "dataset",
                        "bridge": {
                            "left": "allocation_payment",
                            "right": "allocation_campaign",
                            "weight": "weight",
                            "total": "1.00",
                            "tolerance": "0.00",
                            "normalization_scope": {"entities": ["payments"]},
                        },
                    }
                )
            ],
        ),
    ],
    "relationships": [
        rel("customer_zone", "customers", "zones", "zone_id", "zone_id"),
        rel("billing", "payments", "customers", "customer_id", "customer_id"),
        rel("shipping", "payments", "customers", "shipping_id", "customer_id"),
        rel("profile", "profiles", "customers", "customer_id", "customer_id", "one_to_one"),
        rel("manager", "employees", "employees", "manager_id", "employee_id"),
        rel("allocation_payment", "allocations", "payments", "payment_id", "payment_id"),
        rel("allocation_campaign", "allocations", "campaigns", "campaign_id", "campaign_id"),
    ],
    "metrics": [
        {"name": "revenue", "expression": {"sql": "SUM(payments.amount)"}},
        {
            "name": "identified",
            "expression": {"sql": "COUNT(DISTINCT customers.national_id)"},
            "custom_extensions": [
                extension(
                    {
                        "element": "metric",
                        "aggregate_class": "holistic",
                        "additivity": "non_additive",
                    }
                )
            ],
        },
        {"name": "phones", "expression": {"sql": "COUNT(customers.phone)"}},
        {
            "name": "flagged",
            "expression": {
                "sql": "SUM(CASE WHEN customers.national_id IS NOT NULL THEN payments.amount END)"
            },
        },
        {
            "name": "running",
            "expression": {"sql": "SUM(payments.amount)"},
            "custom_extensions": [
                extension(
                    {
                        "element": "metric",
                        "window": {
                            "function": "running_total",
                            "of": "revenue",
                            "partition_by": ["national_id"],
                            "order_by": ["paid_on"],
                            "densify": "none",
                        },
                    }
                )
            ],
        },
        {
            "name": "tails",
            "expression": {"sql": "COUNT(DISTINCT customers.phone_tail)"},
            "custom_extensions": [
                extension(
                    {
                        "element": "metric",
                        "aggregate_class": "holistic",
                        "additivity": "non_additive",
                    }
                )
            ],
        },
    ],
    "custom_extensions": [extension({"element": "model", "default_time_zone": "America/La_Paz"})],
}
CATALOG = {
    "zones": {"zone_id": SourceColumn("BIGINT", False), "label": SourceColumn("VARCHAR")},
    "customers": {
        "customer_id": SourceColumn("BIGINT", False),
        "zone_id": SourceColumn("BIGINT"),
        "national_id": SourceColumn("VARCHAR"),
        "phone": SourceColumn("VARCHAR"),
        "segment": SourceColumn("VARCHAR"),
    },
    "profiles": {"customer_id": SourceColumn("BIGINT", False), "score": SourceColumn("INTEGER")},
    "payments": {
        "payment_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT"),
        "shipping_id": SourceColumn("BIGINT"),
        "amount": SourceColumn("DECIMAL(18,2)"),
        "paid_on": SourceColumn("DATE"),
    },
    "campaigns": {"campaign_id": SourceColumn("BIGINT", False), "title": SourceColumn("VARCHAR")},
    "allocations": {
        "allocation_id": SourceColumn("BIGINT", False),
        "payment_id": SourceColumn("BIGINT"),
        "campaign_id": SourceColumn("BIGINT"),
        "weight": SourceColumn("DECIMAL(9,4)"),
    },
    "employees": {
        "employee_id": SourceColumn("BIGINT", False),
        "manager_id": SourceColumn("BIGINT"),
    },
}


GROUPS = frozenset({"collections", "management", "analysts", "auditors"})
MANAGED_TABLES = {
    "security.user_zone": ("user_id", "zone_id"),
    "security.blocked_zone": ("user_id", "zone_id"),
    "security.registered": ("user_id",),
    "security.org_closure": ("ancestor", "descendant"),
}


@cache
def bank() -> CompiledModel:
    loaded = read(json.dumps(MODEL).encode(), "semantic/bank.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)
