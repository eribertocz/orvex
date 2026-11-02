"""The catalog: what one caller may query (`catalog` v1, P2-10, #309).

A catalog lists the datasets, fields, metrics and relationships the caller may use:
- a member whose effective protection is `hidden` is absent, indistinguishable from one that
  does not exist; the same holds for a metric that inherits `hidden`, a dataset with no
  visible member, and a relationship with a hidden or masked end or key;
- a masked field shows only that it is masked text, nothing of itself;
- a dataset that a policy not yet executable makes unusable is listed by name only, as not
  available, and only when the caller would see one of its members (Owner decision of
  2026-11-02); otherwise it is absent;
- a description that names a protected member is flagged by the compiler and omitted for
  every caller without builder permission.

Roles never widen anything: the catalog follows the caller's groups only (#238).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.catalog import LogicalType
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.extensions import full_match_validator
from orvex.semantic.policies import PolicyModel

SCHEMA_FILE = "catalog-v1.json"


class InvalidCatalog(ValueError):
    """A catalog that breaks `catalog` v1: an internal error, never a user's."""


@cache
def schema_validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.semantic.catalog").joinpath(SCHEMA_FILE).read_bytes())
    return full_match_validator(schema)


def _items(node: object, key: str) -> list[Any]:
    value = node.get(key, ()) if isinstance(node, Mapping) else ()
    return list(value) if isinstance(value, list | tuple) else []


def _logical(logical: LogicalType) -> dict[str, object]:
    family = logical.datatype or "Opaque"
    found: dict[str, object] = {"family": family}
    if family == "Decimal":
        if logical.precision is not None and logical.scale is not None:
            found.update(precision=logical.precision, scale=logical.scale)
    elif family == "Integer":
        if logical.width is not None and logical.signed is not None:
            found.update(width=logical.width, signed=logical.signed)
    elif family in {"DateTime", "DateTimeTz"} and logical.zone:
        found["zone"] = logical.zone
    return found


def protected_names(compiled: CompiledModel, policies: PolicyModel) -> list[str]:
    """The names, alone and as `dataset.member`, of every member that is not visible to some
    caller: protected by its own policy or by inheritance. With no groups a caller has the
    strictest view, since groups only widen it."""
    names: set[str] = set()
    for member in compiled.members:
        if policies.protection(member, ()) != "visible":
            names.add(member.name.lower())
            if member.dataset:
                names.add(f"{member.dataset.lower()}.{member.name.lower()}")
    return sorted(names)


def flagged(description: str, protected: Collection[str]) -> bool:
    """Whether a description names a protected member (whole words, any case): the compile
    report's warning, and the reason it is omitted for ordinary callers."""
    text = description.lower()
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)
        for name in protected
    )


def _describe(
    node: Mapping[str, Any], builder: bool, protected: Collection[str]
) -> dict[str, object]:
    description = node.get("description")
    if not isinstance(description, str) or not description:
        return {}
    if not flagged(description, protected):
        return {"description": description}
    return {"description": description, "description_flagged": True} if builder else {}


def catalog_for(
    compiled: CompiledModel,
    policies: PolicyModel,
    *,
    groups: Collection[str],
    builder: bool,
) -> dict[str, Any]:
    """The catalog of a caller in `groups`; `builder` is their builder permission, which shows
    flagged descriptions with their warning and nothing else."""
    groups = frozenset(groups)
    document = compiled.ossie.document
    payloads = {
        e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
        for e in compiled.ossie.product_extensions
    }
    protected = protected_names(compiled, policies)

    def level(member: MemberId) -> str:
        return policies.protection(member, groups)

    def member(
        node: Mapping[str, Any], member_id: MemberId, pointer: str
    ) -> dict[str, object] | None:
        protection = level(member_id)
        if protection == "hidden" or member_id not in compiled.members:
            return None
        name = str(node["name"])
        if protection == "masked":
            return {"name": name, "masked": True, "encoding": "VARCHAR"}
        typed = compiled.members[member_id].typed.type
        payload = payloads.get(pointer, {})
        found: dict[str, object] = {"name": name, **_describe(node, builder, protected)}
        found["logical"] = _logical(typed.logical)
        if typed.semantic_type:
            found["semantic_type"] = typed.semantic_type
        for key in ("unit", "currency"):
            if isinstance(payload.get(key), str):
                found[key] = payload[key]
        found["exactness"] = typed.exactness
        found["nullable"] = typed.nullable
        return found

    datasets: list[dict[str, object]] = []
    listed: set[str] = set()
    for at, dataset in enumerate(_items(document, "datasets")):
        name = sql.normalize(str(dataset["name"]))
        fields = [
            m
            for index, node in enumerate(_items(dataset, "fields"))
            if (
                m := member(
                    node,
                    MemberId("field", name, sql.normalize(str(node["name"]))),
                    f"/datasets/{at}/fields/{index}",
                )
            )
        ]
        metrics = [
            m
            for index, node in enumerate(_items(dataset, "metrics"))
            if (
                m := member(
                    node,
                    MemberId("metric", name, sql.normalize(str(node["name"]))),
                    f"/datasets/{at}/metrics/{index}",
                )
            )
        ]
        if not fields and not metrics:
            continue  # nothing visible: absent, like a dataset that does not exist
        if name in policies.unusable:
            datasets.append(
                {"name": str(dataset["name"]), "available": False, "reason": "not_available_yet"}
            )
            continue
        entry: dict[str, object] = {
            "name": str(dataset["name"]),
            **_describe(dataset, builder, protected),
            "available": True,
        }
        grain = payloads.get(f"/datasets/{at}", {}).get("grain")
        dimensions = (
            [str(d) for d in _items(grain, "dimensions")] if isinstance(grain, Mapping) else []
        )
        # The grain is listed whole or not at all: a partial grain would misstate it, and a
        # hidden dimension must never be named.
        if dimensions and all(
            level(MemberId("field", name, sql.normalize(d))) == "visible" for d in dimensions
        ):
            entry["grain"] = dimensions
        entry["fields"] = fields
        entry["metrics"] = metrics
        datasets.append(entry)
        listed.add(name)
    model_metrics = [
        m
        for index, node in enumerate(_items(document, "metrics"))
        if (
            m := member(
                node,
                MemberId("metric", None, sql.normalize(str(node["name"]))),
                f"/metrics/{index}",
            )
        )
        and not any(
            d.dataset in policies.unusable
            for d in compiled.members[
                MemberId("metric", None, sql.normalize(str(node["name"])))
            ].parsed.lineage
        )
    ]
    relationships = []
    for at, relationship in enumerate(_items(document, "relationships")):
        left, right = (
            sql.normalize(str(relationship["from"])),
            sql.normalize(str(relationship["to"])),
        )
        keys = [
            MemberId("field", left, sql.normalize(str(c)))
            for c in _items(relationship, "from_columns")
        ]
        keys += [
            MemberId("field", right, sql.normalize(str(c)))
            for c in _items(relationship, "to_columns")
        ]
        if left in listed and right in listed and all(level(k) == "visible" for k in keys):
            cardinality = payloads.get(f"/relationships/{at}", {}).get("cardinality", "many_to_one")
            relationships.append(
                {
                    "name": str(relationship["name"]),
                    "from": str(relationship["from"]),
                    "to": str(relationship["to"]),
                    "cardinality": cardinality,
                }
            )
    body = {"datasets": datasets, "metrics": model_metrics, "relationships": relationships}
    catalog = {
        "kind": "catalog",
        "version": 1,
        "model_fingerprint": compiled.model_fingerprint,
        "catalog_fingerprint": fingerprint("catalog", 1, body),
        **body,
    }
    problems = list(schema_validator().iter_errors(catalog))
    if problems:
        raise InvalidCatalog(problems[0].message)
    return catalog


def catalog_for_context(
    compiled: CompiledModel, policies: PolicyModel, context: Mapping[str, Any]
) -> dict[str, Any]:
    """The catalog of the caller a `security-context` v1 document describes. The document must
    be the one built for this request (`current_fingerprint`, P2-09); a malformed one is
    refused, never read as a caller with fewer or more permissions."""
    from orvex.contracts.security_context.v1 import SecurityContext

    try:
        parsed = SecurityContext.model_validate(dict(context))
    except ValueError as error:
        raise InvalidCatalog("not a security-context v1 document") from error
    groups = [str(getattr(group, "root", group)) for group in parsed.groups]
    if groups != sorted(set(groups)):
        raise InvalidCatalog("the context's groups are not normalized")
    return catalog_for(compiled, policies, groups=groups, builder=parsed.role == "builder")
