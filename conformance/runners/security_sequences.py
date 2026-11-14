"""A `sequence` case of questions, run as its callers would live it (#444, #447): each step's
caller context built anew from the current state (its groups, the policies and the mapping
snapshot now), exactly as the server builds one per request, and each step's change applied
before it. Every step shares one result cache, so a later step is served from an earlier one's
entry only when nothing it depends on changed.

Changes: `remove_group` (the user's groups from then on), `edit_metric` (the model recompiled
with the metric's new expression), `replace_policy` and `replace_column_policy` (the policies
as a Builder edited them, read again) and `delete_row` (on the engine that serves the dataset, by
its owner; an existing DuckLake then verifies the new snapshot on its schedule). The steps that
exist only over HTTP (`next-page`, `async-start`, `async-result`) are `security_http`'s.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import yaml
from orvex.access.context import permission_fingerprint
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.semantic.result_cache import ResultCache

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from conformance.runners.security_engines import Received, Served, ask


def run(
    found: sf.Fixture, place: Served, request: Mapping[str, Any], changes: bool = True
) -> list[Received]:
    """Every step's answer, in order; `changes`: apply the steps' changes, or run them as if
    nothing changed (a case's control)."""
    place.cache = ResultCache()
    removed: dict[str, set[str]] = {}
    answers = []
    for step in request["steps"]:
        change = step.get("change") if changes else None
        if change is not None:
            found = _change(found, place, change, removed)
        if step["request"]["kind"] == "lake-change":  # a change as a step of its own
            if changes:
                found = _change(found, place, step["request"]["change"], removed)
            continue
        try:
            caller = context_for(found, place, step["caller"], removed)
        except SemanticError as error:  # the changed policies do not read: the refusal
            text = json.dumps({"code": error.code, "details": dict(error.details)})
            answers.append(Received(None, [], [], error.code, [text]))
            continue
        answers.append(ask(found, place, step["request"], caller))
    return answers


def _change(
    found: sf.Fixture, place: Served, change: Mapping[str, Any], removed: dict[str, set[str]]
) -> sf.Fixture:
    if change["op"] == "remove_group":
        removed.setdefault(str(change["user"]), set()).add(str(change["group"]))
        return found
    if change["op"] == "edit_metric":

        def edited(model: dict[str, Any]) -> None:
            for metric in model["metrics"]:
                if metric["name"] == change["metric"]:
                    metric["expression"] = {"sql": change["sql"]}

        return replace(found, compiled=compile_with(found, edited))
    if change["op"] in {"replace_policy", "replace_column_policy"}:
        return replace(found, policies_text=yaml.safe_dump(changed_policies(found, change)))
    if change["op"] == "delete_row":
        assert place.delete is not None, "this engine cannot change its data"
        place.delete(str(change["table"]), change["row"])
        return found
    if place.lake_change is not None:  # an existing DuckLake's owner, or its host
        place.lake_change(change)
        return found
    raise RuntimeError(f"no change {change['op']!r} on this engine")


def changed_policies(found: sf.Fixture, change: Mapping[str, Any]) -> dict[str, Any]:
    """The fixture's `security.yaml` with one policy replaced, as a Builder would edit it."""
    document: dict[str, Any] = yaml.safe_load(
        found.policies_text or (found.folder / "security.yaml").read_text("utf-8")
    )
    if change["op"] == "replace_policy":
        (rule,) = [r for r in document["row_policies"] if r["name"] == change["policy"]]
        rule["predicate"] = change["predicate"]
        return document
    rules = [r for r in document.get("column_policies", []) if r["member"] != change["member"]]
    added = {"member": change["member"], "default": change["default"]}
    document["column_policies"] = [
        *rules,
        added | ({"mask": change["mask"]} if "mask" in change else {}),
    ]
    return document


def compile_with(found: sf.Fixture, edit: Callable[[dict[str, Any]], object]) -> Any:  # noqa: ANN401
    """The fixture's model, edited in place by `edit`, compiled."""
    text = (found.folder / "model.ossie.json").read_text("utf-8")
    model = json.loads(text.replace("{{vendor}}", ce.VENDORS[0]))
    edit(model)
    loaded = read(json.dumps(model).encode(), "model.ossie.json", ce.VENDORS)
    return compile_model(loaded, found.catalog, ce.VENDORS)


def context_for(
    found: sf.Fixture, place: Served, caller: Mapping[str, Any], removed: dict[str, set[str]]
) -> dict[str, Any]:
    """The caller's context for this step: from the case's caller, its removed groups gone, its
    fingerprint built from the policies and the mapping snapshot in force now."""
    groups = sorted(set(caller["groups"]) - removed.get(str(caller["user"]), set()))
    policies = found.policies(groups)
    snapshot = str(place.lake.current_snapshot().id)
    fingerprint = permission_fingerprint(
        user=caller["user"],
        project=caller["project"],
        role=caller["role"],
        groups=groups,
        attributes=caller["attributes"],
        policy_fingerprint=policies.fingerprint,
        mapping_snapshots=dict.fromkeys(sorted(policies.managed_reads), snapshot),
    )
    return {**caller, "groups": groups, "permission_fingerprint": fingerprint}
