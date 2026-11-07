"""What the engine would do with a question, and why, with no data read (P2-27b, #364).

`explain` runs `prepare`: binding, `analyze`, the cursor's snapshot, `plan`, the adapter,
discharge (C9) and the evidence relied on. It never calls `engine.read`, and never checks
zones, which would read. Its document is `semantic-explain` v1:

- **Two kinds of failure.** An error of category `query` or `security` (an invalid query, an
  unknown or hidden member, a masked one, a cursor) is raised, as `answer` raises it: a hidden
  member is never explained, so nothing a policy hides is confirmed. An error of category
  `correctness`, `budget` or `engine` is the plan's own refusal, and is explained:
  `outcome: refused`, its message in the caller's audience, and every part reached before it.
- **Parts by stage.** `stage` names the last stage reached (`received`, `bound`, `analyzed`,
  `planned`); each part appears from the stage that produces it on.
- **The receipt's parts, the receipt's way.** The question, the plan and the evidence come from
  `receipt.building.explanation_view`, so they are built and restricted exactly as the receipt
  of the same question shows them. The aggregate table and the estimate are builder-only, as
  in the receipt's builder view, and the estimate is left out whenever a row policy applies
  (#238 rule 12).
- **Never** any SQL, statement text, parameter value or data value, for any audience.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.errors import SemanticError
from orvex.semantic.estimates import estimate
from orvex.semantic.evidence import CONSISTENT, relied
from orvex.semantic.execution import Engine, Progress, prepare, question_of
from orvex.semantic.ossie.extensions import full_match_validator
from orvex.semantic.policies import PolicyModel, read_caller
from orvex.semantic.receipt.building import explanation_view

SCHEMA_FILE = "semantic-explain-v1.json"
EXPLAINED = frozenset({"correctness", "budget", "engine"})  # the plan's own refusals
LOCALE = "es"


class InvalidExplanation(ValueError):
    """An explanation that does not fit its contract: never sent."""


@cache
def schema_validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.semantic").joinpath(SCHEMA_FILE).read_bytes())
    return full_match_validator(schema)


def _instant(value: object) -> str:
    moment = datetime.fromisoformat(str(value)).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _snapshot(engine: Engine) -> dict[str, object]:
    shown: dict[str, object] = {"id": int(str(engine.snapshot["id"]))}
    if engine.snapshot.get("committed_at") is not None:
        shown["committed_at"] = _instant(engine.snapshot["committed_at"])
    return shown


def _materialization(progress: Progress, rollup: str | None) -> dict[str, object]:
    """The aggregate table read in place of its base, or why none is."""
    if rollup is not None:
        return {"aggregate_table": rollup}
    report, ledger = progress.report, progress.ledger
    eligible = [c for c in (report.materials if report is not None else ()) if c.eligible]
    if not eligible:
        return {"reason": "no_eligible_table"}
    if ledger is not None and not any(
        ledger.verified(("dataset", c.dataset, CONSISTENT)) for c in eligible
    ):
        return {"reason": "not_verified_consistent"}
    return {"reason": "no_eligible_table"}


def explain(
    query: str | bytes,
    compiled: CompiledModel,
    policies: PolicyModel,
    context: Mapping[str, Any],
    engine: Engine,
    viewer: object,
) -> dict[str, Any]:
    """The `semantic-explain` v1 document of `query` for the caller `context` describes, seen by
    `viewer`; or the `SemanticError` `answer` would raise for an error that is not the plan's
    own refusal."""
    caller = read_caller(context)
    builder = caller.builder
    progress = Progress()
    document: dict[str, Any] = {
        "kind": "semantic_explain",
        "version": 1,
        "model_fingerprint": compiled.model_fingerprint,
    }
    refusal: SemanticError | None = None
    ready = None
    try:
        ready = prepare(query, compiled, policies, context, engine, progress, raise_violated=False)
    except SemanticError as error:
        if error.category not in EXPLAINED:
            raise
        refusal = error
    used = progress.engine or engine
    document["snapshot"] = _snapshot(used)
    document["stage"] = progress.stage
    if progress.bound is not None:
        document["query_fingerprint"] = progress.bound.fingerprint
    report = progress.report
    plan_ = ready.plan if ready is not None else None
    shown = explanation_view(
        question_document=question_of(progress.bound, compiled) if progress.bound else None,
        obligations=(
            [o.for_receipt(ready.evidence[o.id]) for o in report.obligations]
            if ready is not None and report is not None
            else [o.for_receipt(None) for o in report.obligations]
            if report is not None
            else None
        ),
        plan_fingerprint=plan_.fingerprint if plan_ is not None else None,
        evidence=ready.uses if ready is not None else None,
        viewer=viewer,
    )
    document.update(shown)
    if ready is not None and progress.ledger is not None:
        try:  # relied-on evidence violated: `answer` would refuse before any read
            progress.ledger.uses(relied(ready.plan))
        except SemanticError as error:
            refusal = error
    if ready is not None and builder:
        rollup = plan_.rollup.name if plan_ is not None and plan_.rollup is not None else None
        document["materialization"] = _materialization(progress, rollup)
        restricted = bool(plan_.placement and plan_.placement.applied) if plan_ else False
        if not restricted:
            snapshot = ready.engine.snapshot.get("id")
            figures = (
                estimate(plan_, ready.physical, compiled, ready.engine.statistics, snapshot)
                if isinstance(snapshot, int) and not isinstance(snapshot, bool)
                else None
            )
            if figures is not None:
                document["estimate"] = figures
    if refusal is not None:
        document["outcome"] = "refused"
        audience = "builder" if builder else "other"
        document["refusal"] = refusal.to_message(LOCALE, audience).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    else:
        document["outcome"] = "planned"
    problems = list(schema_validator().iter_errors(document))
    if problems:
        where = "/".join(str(part) for part in problems[0].absolute_path)
        raise InvalidExplanation(f"/{where}: {problems[0].message}")
    return document
