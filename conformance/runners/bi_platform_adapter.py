"""The only place that knows bi-platform's corpus format (P0-11).

bi-platform wrote its corpus as prose, CSV files and expected results, and one Apache Ossie model
per case with `custom_extensions` under its own vendor name. This adapter reads that format and
translates what the product needs today: CSV values, the vendor alias and the error codes.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

VENDOR_NAME = "bi-platform"

# bi-platform error code -> the product's error code (docs/architecture/semantic-engine.md §12).
# Every expected error code of an imported case must appear here; an unmapped code fails the load.
ERROR_CODES = {
    "AMBIGUOUS_RELATIONSHIP_PATH": "AMBIGUOUS_RELATIONSHIP_PATH",
    "ILLEGAL_TEMPORAL_REAGGREGATION": "ILLEGAL_TEMPORAL_REAGGREGATION",
    "INCOMPATIBLE_UNITS": "INCOMPATIBLE_UNITS",
}

# bi-platform manifest outcome -> what the case expects.
OUTCOMES = {"result": "result", "compile_error": "error"}

type Row = dict[str, str | None]
type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


def read_csv(path: Path) -> list[Row]:
    """Header row, comma separator, no quoting; an empty field is NULL; values stay text.

    Values are never converted to numbers, so a decimal keeps the scale it was written with.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, quoting=csv.QUOTE_NONE, strict=True)
        header = next(reader)
        rows: list[Row] = []
        for line in reader:
            if len(line) != len(header):
                message = f"{path.name}: a row has {len(line)} fields, the header {len(header)}"
                raise ValueError(message)
            rows.append({name: value or None for name, value in zip(header, line, strict=True)})
    return rows


def load_yaml(path: Path) -> Json:
    loaded: Json = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def vendor_extensions(model: Json) -> list[Json]:
    """Every `custom_extensions` payload written under bi-platform's vendor name.

    They are kept aside until `ossie-extensions` v1 (P2-01) defines the product's own payloads they
    translate to. Entries of any other vendor are ignored, as Ossie requires.
    """
    return list(_walk_extensions(model))


def _walk_extensions(node: Json) -> Iterator[Json]:
    if isinstance(node, dict):
        extensions = node.get("custom_extensions")
        if isinstance(extensions, list):
            for entry in extensions:
                if isinstance(entry, dict) and entry.get("vendor_name") == VENDOR_NAME:
                    yield entry.get("data")
        for value in node.values():
            yield from _walk_extensions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_extensions(item)


def product_error_code(code: str) -> str | None:
    return ERROR_CODES.get(code)


# --- Translation into the product's `ossie-extensions` v1 payloads (P2-01, #276) ---------------
#
# Every key bi-platform writes is mapped below; a key with no mapping fails the translation
# (UntranslatableKey), so nothing is dropped silently. Row policies go to P2-08 and are returned
# apart. The vendor token of the product's entries is passed in, never written here.

AXIS_BEHAVIORS = {
    "additive": "additive",
    "distinct_recompute": "recompute",
    "forbidden": "forbidden",
    "period_end": "period_end",
}
ADDITIVITY = {
    "fully_additive": "additive",
    "semi_additive": "semi_additive",
    "non_additive": "non_additive",
}
FIELD_TIME_ROLES = ("event", "snapshot", "validity_start", "validity_end")


class UntranslatableKey(ValueError):
    """A bi-platform payload key, or value, with no mapping to `ossie-extensions` v1."""


def _payload(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), str):
        raise UntranslatableKey(f"not a payload entry: {entry!r}")
    data = json.loads(str(entry["data"]))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise UntranslatableKey(f"unknown payload version: {data!r}")
    return data


def _own(node: dict[str, Any]) -> dict[str, Any] | None:
    payloads = [
        _payload(entry)
        for entry in node.get("custom_extensions") or []
        if isinstance(entry, dict) and entry.get("vendor_name") == VENDOR_NAME
    ]
    if len(payloads) > 1:
        raise UntranslatableKey("two bi-platform entries on one element")
    return payloads[0] if payloads else None


def _take(data: dict[str, Any], known: set[str]) -> None:
    unknown = set(data) - known - {"version"}
    if unknown:
        raise UntranslatableKey(f"no mapping for {sorted(unknown)}")


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UntranslatableKey(f"not a list of names: {value!r}")
    return [str(item) for item in value]


def translate(model: dict[str, Any], vendor_name: str) -> tuple[dict[str, Any], list[Any]]:
    """The model with every bi-platform payload replaced by a product payload under
    `vendor_name`, and the row policies set aside for P2-08."""
    translated: dict[str, Any] = json.loads(json.dumps(model))
    datasets = [d for d in translated.get("datasets") or [] if isinstance(d, dict)]
    payloads: dict[int, dict[str, Any]] = {}  # id(element) -> product payload
    policies: list[Any] = []

    def put(element: dict[str, Any], kind: str, members: dict[str, Any]) -> None:
        payloads[id(element)] = {"version": 1, "element": kind, **members}

    model_data = _own(translated)
    bridges: list[Any] = []
    if model_data is not None:
        _take(model_data, {"semantic_version", "bridges", "policies"})
        bridges = list(model_data.get("bridges") or [])
        policies = list(model_data.get("policies") or [])
        put(translated, "model", {})

    time_dimensions: set[str] = set()
    for dataset in datasets:
        members: dict[str, Any] = {}
        data = _own(dataset) or {}
        _take(data, {"entity", "grain", "keys", "measures"})
        if "entity" in data:
            members["entity"] = data["entity"]
        if "grain" in data:
            grain = data["grain"]
            if not isinstance(grain, dict) or set(grain) != {"dimensions"}:
                raise UntranslatableKey(f"grain {grain!r}")
            members["grain"] = {"dimensions": _names(grain["dimensions"])}
        if "keys" in data:
            ossie_keys = [_names(dataset.get("primary_key") or [])] + [
                _names(key) for key in dataset.get("unique_keys") or []
            ]
            entity_keys: list[Any] = []
            for key in data["keys"]:
                fields = _names(key["fields"])
                entry: dict[str, Any] = {"entity": key["entity"], "fields": fields}
                if fields not in ossie_keys:
                    entry["coarser"] = True
                entity_keys.append(entry)
            members["entity_keys"] = entity_keys
        if "measures" in data:
            members["aggregate_table"] = {
                "measures": [
                    {"name": m["name"], "expression": m["expression"]} for m in data["measures"]
                ]
            }
        roles: dict[str, str] = {}
        for field in dataset.get("fields") or []:
            field_members = _translate_field(field, roles)
            if field_members is not None:
                if "time_zone" in field_members or "time_role_seen" in field_members:
                    time_dimensions.add(str(field_members.get("conformed_dimension", "")))
                field_members.pop("time_role_seen", None)
                put(field, "field", field_members)
        time_roles: dict[str, Any] = {}
        for role in ("event", "snapshot"):
            if role in roles:
                time_roles[role] = roles[role]
        if "validity_start" in roles or "validity_end" in roles:
            if not ("validity_start" in roles and "validity_end" in roles):
                raise UntranslatableKey("a validity needs both its start and its end")
            time_roles["validity"] = {"from": roles["validity_start"], "to": roles["validity_end"]}
        if time_roles:
            members["time_roles"] = time_roles
        if members or _own(dataset) is not None:
            put(dataset, "dataset", members)

    for bridge in bridges:
        target = next((d for d in datasets if d.get("name") == bridge["bridge"]), None)
        if target is None:
            raise UntranslatableKey(f"bridge dataset {bridge['bridge']!r} not found")
        allocation = bridge["allocation"]
        payloads.setdefault(id(target), {"version": 1, "element": "dataset"})["bridge"] = {
            "left": bridge["left_relationship"],
            "right": bridge["right_relationship"],
            "weight": allocation["weight"],
            "total": allocation["required_total"],
            "tolerance": allocation["tolerance"],
            "normalization_scope": {
                "entities": _names(allocation["normalization_scope"]["entities"])
            },
        }

    for relationship in translated.get("relationships") or []:
        relationship_data = _own(relationship)
        if relationship_data is None:
            continue
        _take(relationship_data, {"optionality", "temporal"})
        put(
            relationship,
            "relationship",
            {
                k: relationship_data[k]
                for k in ("optionality", "temporal")
                if k in relationship_data
            },
        )

    metric_lists = [translated.get("metrics") or []] + [d.get("metrics") or [] for d in datasets]
    for metrics in metric_lists:
        for metric in metrics:
            metric_data = _own(metric)
            if metric_data is not None:
                put(metric, "metric", _translate_metric(metric, metric_data, time_dimensions))

    def replace(node: object) -> None:
        if isinstance(node, dict):
            if id(node) in payloads:
                kept = [
                    e
                    for e in node.get("custom_extensions") or []
                    if isinstance(e, dict) and e.get("vendor_name") != VENDOR_NAME
                ]
                kept.append(
                    {
                        "vendor_name": vendor_name,
                        "data": json.dumps(payloads[id(node)], sort_keys=True),
                    }
                )
                node["custom_extensions"] = kept
            elif isinstance(node.get("custom_extensions"), list):
                node["custom_extensions"] = [
                    e
                    for e in node["custom_extensions"]
                    if not (isinstance(e, dict) and e.get("vendor_name") == VENDOR_NAME)
                ]
                if not node["custom_extensions"]:
                    del node["custom_extensions"]
            for value in list(node.values()):
                replace(value)
        elif isinstance(node, list):
            for item in node:
                replace(item)

    replace(translated)
    return translated, policies


def _translate_field(field: dict[str, Any], roles: dict[str, str]) -> dict[str, Any] | None:
    data = _own(field)
    if data is None:
        return None
    _take(data, {"dimension", "semantic_role", "time", "time_role", "unit"})
    members: dict[str, Any] = {}
    if "dimension" in data:
        members["conformed_dimension"] = data["dimension"]
    if "time" in data:
        time = data["time"]
        if not isinstance(time, dict) or set(time) - {"timezone", "tzdb"}:
            raise UntranslatableKey(f"time {time!r}")
        zone: dict[str, Any] = {"name": time["timezone"]}
        if "tzdb" in time:
            zone["database_version"] = time["tzdb"]
        members["time_zone"] = zone
    if "semantic_role" in data:
        if data["semantic_role"] != "business_time":
            raise UntranslatableKey(f"semantic_role {data['semantic_role']!r}")
        members["business_time"] = True
    if "time_role" in data:
        role = str(data["time_role"])
        if role not in FIELD_TIME_ROLES:
            raise UntranslatableKey(f"time_role {role!r}")
        roles[role] = str(field["name"])
        members["time_role_seen"] = True
    if "unit" in data:
        unit = data["unit"]
        if not isinstance(unit, dict) or set(unit) != {"currency"}:
            raise UntranslatableKey(f"unit {unit!r}")
        members["semantic_type"] = "amount"
        members["currency"] = unit["currency"]
    return members


def _translate_metric(
    metric: dict[str, Any], data: dict[str, Any], time_dimensions: set[str]
) -> dict[str, Any]:
    _take(data, {"aggregation", "allocation_bridge", "dataset", "grain", "time_behavior", "unit"})
    members: dict[str, Any] = {}
    if "aggregation" in data:
        aggregation = data["aggregation"]
        if not isinstance(aggregation, dict) or set(aggregation) - {"class", "axes"}:
            raise UntranslatableKey(f"aggregation {aggregation!r}")
        if aggregation["class"] not in ADDITIVITY:
            raise UntranslatableKey(f"aggregation class {aggregation['class']!r}")
        members["additivity"] = ADDITIVITY[str(aggregation["class"])]
        if "axes" in aggregation:
            axes: list[Any] = []
            for axis in aggregation["axes"]:
                behavior = AXIS_BEHAVIORS.get(str(axis["behavior"]))
                if behavior is None:
                    raise UntranslatableKey(f"axis behavior {axis['behavior']!r}")
                side = "dimension" if "dimension" in axis else "entity"
                axes.append({side: axis[side], "behavior": behavior})
            members["non_additive_axes"] = axes
    if "time_behavior" in data:
        behavior = data["time_behavior"]
        if not isinstance(behavior, dict) or behavior.get("rule") != "period_end":
            raise UntranslatableKey(f"time_behavior {behavior!r}")
        if set(behavior) - {"axis", "rule", "order_by", "selection_scope", "ties"}:
            raise UntranslatableKey(f"time_behavior {sorted(behavior)}")
        period_end: dict[str, Any] = {"order_by": [item["field"] for item in behavior["order_by"]]}
        if "selection_scope" in behavior:
            period_end["per_entities"] = _names(behavior["selection_scope"]["entities"])
        if "ties" in behavior:
            period_end["tie_breaker"] = behavior["ties"]["unique_by_key"]
        members["period_end"] = period_end
    if "allocation_bridge" in data:
        members["allocation"] = {"bridge": data["allocation_bridge"]}
    if "dataset" in data:
        members["home_dataset"] = data["dataset"]
    if "unit" in data:
        if data["unit"] != "ratio":
            raise UntranslatableKey(f"metric unit {data['unit']!r}")
        members["unit"] = "1"
    if "grain" in data:
        # A metric over metrics at a declared grain (CASE-010): S7 with explicit alignment.
        grain = _names(data["grain"]["dimensions"])
        times = [dimension for dimension in grain if dimension in time_dimensions]
        if len(times) != 1:
            raise UntranslatableKey(f"grain {grain} has {len(times)} time dimensions, not one")
        expression = _ossie_expression(metric)
        members["over_metrics"] = {
            "expression": expression,
            "align": {"grain": grain, "time": times[0]},
        }
    return members


def _ossie_expression(element: dict[str, Any]) -> str:
    expression = element.get("expression")
    if isinstance(expression, dict):
        if isinstance(expression.get("sql"), str):
            return str(expression["sql"])
        for dialect in expression.get("dialects") or []:
            if isinstance(dialect, dict) and dialect.get("dialect") == "OSSIE_SQL_2026":
                return str(dialect["expression"])
    raise UntranslatableKey(f"no OSSIE_SQL_2026 expression on {element.get('name')!r}")
