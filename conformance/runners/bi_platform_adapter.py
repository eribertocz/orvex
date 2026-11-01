"""The only place that knows bi-platform's corpus format (P0-11).

bi-platform wrote its corpus as prose, CSV files and expected results, and one Apache Ossie model
per case with `custom_extensions` under its own vendor name. This adapter reads that format and
translates what the product needs today: CSV values, the vendor alias and the error codes.
"""

from __future__ import annotations

import csv
import json
import re
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
CORPUS_SEMANTIC_VERSION = "0.1.0"
FIELD_TIME_ROLES = ("event", "snapshot", "validity_start", "validity_end")


class UntranslatableKey(ValueError):
    """A bi-platform payload key, or value, with no mapping to `ossie-extensions` v1."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys are unique: `json.loads` alone keeps the last of two."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise UntranslatableKey(
            f"a payload repeats a key: {sorted(k for k in set(keys) if keys.count(k) > 1)}"
        )
    return dict(pairs)


def _unique_names(names: list[object], kind: str) -> None:
    repeated = sorted({str(name) for name in names if names.count(name) > 1})
    if repeated:
        raise UntranslatableKey(f"two {kind}s named {repeated}")


def _payload(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), str):
        raise UntranslatableKey(f"not a payload entry: {entry!r}")
    data = json.loads(str(entry["data"]), object_pairs_hook=_unique_keys)
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


def _shape(
    value: object,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
    where: str = "",
) -> dict[str, Any]:
    """`value` as an object with exactly these keys: a missing or any other key fails."""
    if not isinstance(value, dict):
        raise UntranslatableKey(f"{where}: not an object: {value!r}")
    missing = required - set(value)
    unknown = set(value) - required - set(optional)
    if missing or unknown:
        raise UntranslatableKey(
            f"{where}: missing {sorted(missing)}, no mapping for {sorted(unknown)}"
        )
    return value


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
        # The corpus version this adapter maps; another version may mean other things.
        if model_data.get("semantic_version", CORPUS_SEMANTIC_VERSION) != CORPUS_SEMANTIC_VERSION:
            raise UntranslatableKey(f"semantic_version {model_data['semantic_version']!r}")
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
            grain = _shape(data["grain"], {"dimensions"}, where="dataset grain")
            members["grain"] = {"dimensions": _names(grain["dimensions"])}
        if "keys" in data:
            ossie_keys = [_names(dataset.get("primary_key") or [])] + [
                _names(key) for key in dataset.get("unique_keys") or []
            ]
            entity_keys: list[Any] = []
            for written in data["keys"]:
                key = _shape(written, {"entity", "fields"}, where="dataset keys")
                fields = _names(key["fields"])
                entry: dict[str, Any] = {"entity": key["entity"], "fields": fields}
                if fields not in ossie_keys:
                    entry["coarser"] = True
                entity_keys.append(entry)
            members["entity_keys"] = entity_keys
        if "measures" in data:
            members["aggregate_table"] = {
                "measures": [
                    dict(_shape(m, {"name", "expression"}, where="dataset measures"))
                    for m in data["measures"]
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

    # Every lookup by name refuses a repeated name: a later element would replace an earlier one.
    _unique_names([d.get("name") for d in datasets], "dataset")
    relationships = {
        str(r.get("name")): r for r in translated.get("relationships") or [] if isinstance(r, dict)
    }
    _unique_names(
        [r.get("name") for r in translated.get("relationships") or [] if isinstance(r, dict)],
        "relationship",
    )
    bridge_datasets: dict[str, str] = {}  # bi-platform bridge name -> bridge dataset
    for written in bridges:
        bridge = _shape(
            written,
            {
                "name",
                "left",
                "bridge",
                "right",
                "left_relationship",
                "right_relationship",
                "allocation",
            },
            where="bridge",
        )
        if str(bridge["name"]) in bridge_datasets:
            raise UntranslatableKey(f"two bridges named {bridge['name']!r}")
        target = next((d for d in datasets if d.get("name") == bridge["bridge"]), None)
        if target is None:
            raise UntranslatableKey(f"bridge dataset {bridge['bridge']!r} not found")
        # `left` and `right` repeat the two relationships' ends: checked, then dropped.
        for side, relationship_name in (
            ("left", "left_relationship"),
            ("right", "right_relationship"),
        ):
            relationship = relationships.get(str(bridge[relationship_name]))
            if relationship is None or relationship.get("from") != bridge["bridge"]:
                raise UntranslatableKey(
                    f"bridge {relationship_name} must start at the bridge dataset"
                )
            if relationship.get("to") != bridge[side]:
                raise UntranslatableKey(
                    f"bridge {side} {bridge[side]!r} is not where {relationship_name} ends"
                )
        allocation = _shape(
            bridge["allocation"],
            {"weight", "normalization_scope", "required_total", "tolerance"},
            where="bridge allocation",
        )
        scope = _shape(allocation["normalization_scope"], {"entities"}, where="normalization_scope")
        bridge_datasets[str(bridge["name"])] = str(bridge["bridge"])
        target_payload = payloads.setdefault(id(target), {"version": 1, "element": "dataset"})
        if "bridge" in target_payload:
            raise UntranslatableKey(f"two bridges on the bridge dataset {bridge['bridge']!r}")
        target_payload["bridge"] = {
            "left": bridge["left_relationship"],
            "right": bridge["right_relationship"],
            "weight": allocation["weight"],
            "total": allocation["required_total"],
            "tolerance": allocation["tolerance"],
            "normalization_scope": {"entities": _names(scope["entities"])},
        }

    for relationship in translated.get("relationships") or []:
        relationship_data = _own(relationship)
        if relationship_data is None:
            continue
        _take(relationship_data, {"optionality", "temporal"})
        if "temporal" in relationship_data:
            temporal = _shape(
                relationship_data["temporal"],
                {"event_time", "valid_from", "valid_to", "match"},
                {"boundary"},
                where="temporal",
            )
            if (
                temporal.get("boundary", "closed_open") != "closed_open"
                or temporal["match"] != "exactly_one"
            ):
                raise UntranslatableKey(f"temporal {temporal!r}")
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
                put(
                    metric,
                    "metric",
                    _translate_metric(metric, metric_data, time_dimensions, bridge_datasets),
                )

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
            elif _own(node) is not None:
                raise UntranslatableKey(
                    "a bi-platform entry on an element this adapter never reads"
                )
            for value in list(node.values()):
                replace(value)
        elif isinstance(node, list):
            for item in node:
                replace(item)

    replace(translated)
    return move_metric_expressions(translated, vendor_name), policies


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
        if role in roles:
            raise UntranslatableKey(f"two fields with time_role {role!r}")
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
    metric: dict[str, Any],
    data: dict[str, Any],
    time_dimensions: set[str],
    bridge_datasets: dict[str, str],
) -> dict[str, Any]:
    _take(data, {"aggregation", "allocation_bridge", "dataset", "grain", "time_behavior", "unit"})
    members: dict[str, Any] = {}
    period_end_axes: list[str] = []
    if "aggregation" in data:
        aggregation = _shape(data["aggregation"], {"class"}, {"axes"}, where="aggregation")
        if aggregation["class"] not in ADDITIVITY:
            raise UntranslatableKey(f"aggregation class {aggregation['class']!r}")
        members["additivity"] = ADDITIVITY[str(aggregation["class"])]
        if "axes" in aggregation:
            axes: list[Any] = []
            for written in aggregation["axes"]:
                side = (
                    "dimension"
                    if isinstance(written, dict) and "dimension" in written
                    else "entity"
                )
                axis = _shape(written, {side, "behavior"}, where="aggregation axis")
                behavior = AXIS_BEHAVIORS.get(str(axis["behavior"]))
                if behavior is None:
                    raise UntranslatableKey(f"axis behavior {axis['behavior']!r}")
                if behavior == "period_end":
                    period_end_axes.append(f"{side}:{axis[side]}")
                axes.append({side: axis[side], "behavior": behavior})
            # Only a semi-additive metric carries axes. On an additive metric every axis adds,
            # and on a non-additive one every axis recomputes: an axis saying so is implied.
            implied = {"additive": "additive", "non_additive": "recompute"}
            if members["additivity"] == "semi_additive":
                members["non_additive_axes"] = axes
            elif any(axis["behavior"] != implied[members["additivity"]] for axis in axes):
                raise UntranslatableKey(
                    f"an axis of a {aggregation['class']} metric must be "
                    f"{implied[members['additivity']]}"
                )
    if "time_behavior" in data:
        rule_of_time = _shape(
            data["time_behavior"],
            {"rule", "order_by"},
            {"axis", "selection_scope", "ties"},
            where="time_behavior",
        )
        if rule_of_time["rule"] != "period_end":
            raise UntranslatableKey(f"time_behavior rule {rule_of_time['rule']!r}")
        if "axis" in rule_of_time:
            # The axis repeats a period_end axis of the aggregation: checked, then dropped.
            axis = _shape(rule_of_time["axis"], {"dimension"}, where="time_behavior axis")
            if f"dimension:{axis['dimension']}" not in period_end_axes:
                raise UntranslatableKey(
                    f"time_behavior axis {axis['dimension']!r} is no period_end axis"
                )
        order_by = [
            _shape(item, {"field"}, where="time_behavior order_by")["field"]
            for item in rule_of_time["order_by"]
        ]
        period_end: dict[str, Any] = {"order_by": order_by}
        if "selection_scope" in rule_of_time:
            scope = _shape(rule_of_time["selection_scope"], {"entities"}, where="selection_scope")
            period_end["per_entities"] = _names(scope["entities"])
        if "ties" in rule_of_time:
            period_end["tie_breaker"] = _shape(
                rule_of_time["ties"], {"unique_by_key"}, where="ties"
            )["unique_by_key"]
        members["period_end"] = period_end
    if "allocation_bridge" in data:
        bridge_dataset = bridge_datasets.get(str(data["allocation_bridge"]))
        if bridge_dataset is None:
            raise UntranslatableKey(
                f"allocation_bridge {data['allocation_bridge']!r} names no bridge"
            )
        members["allocation"] = {"bridge": bridge_dataset}
    if "dataset" in data:
        members["home_dataset"] = data["dataset"]
    if "unit" in data:
        if data["unit"] != "ratio":
            raise UntranslatableKey(f"metric unit {data['unit']!r}")
        members["unit"] = "1"
    if "grain" in data:
        # A metric over metrics at a declared grain (CASE-010): S7 with explicit alignment.
        grain = _names(_shape(data["grain"], {"dimensions"}, where="metric grain")["dimensions"])
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


# A single-quoted string (skipped), or a name of one or two parts, each a double-quoted
# identifier or a word, that is not a function call.
_PART = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_EXPRESSION_TOKEN = re.compile(rf"'(?:[^']|'')*'|((?>{_PART}(?:\.{_PART})?))(?!\s*\()")


def _identifier(part: str) -> str:
    quoted = len(part) >= 2 and part.startswith('"') and part.endswith('"')
    return part[1:-1].replace('""', '"') if quoted else part.casefold()


def move_metric_expressions(model: dict[str, Any], product_dialect: str) -> dict[str, Any]:
    """A copy of `model` where a metric whose OSSIE_SQL_2026 expression names another metric
    (`metric` for a model metric, `dataset.metric` for a dataset metric) carries that text,
    unchanged, as its only entry under `product_dialect`.

    An Ossie metric expression cannot refer to another metric (spec L424, L692-L699); the
    corpus's derived metrics do, so they are written in the product's expression language, whose
    dialect token is the product's vendor name (ossie-extensions v1 README).
    """
    moved: dict[str, Any] = json.loads(json.dumps(model))
    datasets = [d for d in moved.get("datasets") or [] if isinstance(d, dict)]
    metric_names: set[tuple[str, ...]] = {
        (_identifier(str(m["name"])),) for m in moved.get("metrics") or [] if isinstance(m, dict)
    }
    for dataset in datasets:
        metric_names |= {
            (_identifier(str(dataset["name"])), _identifier(str(m["name"])))
            for m in dataset.get("metrics") or []
            if isinstance(m, dict)
        }
    for metrics in [moved.get("metrics") or []] + [d.get("metrics") or [] for d in datasets]:
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            text = _ossie_expression(metric)
            used = {
                tuple(_identifier(part) for part in re.findall(_PART, match.group(1)))
                for match in _EXPRESSION_TOKEN.finditer(text)
                if match.group(1)
            }
            if used & metric_names:
                metric["expression"] = {
                    "dialects": [{"dialect": product_dialect, "expression": text}]
                }
    return moved
