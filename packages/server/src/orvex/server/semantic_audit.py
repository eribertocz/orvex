"""The audit events of a semantic answer, in server mode:
- **`query`** (#445; `security.md` §4): one per synchronous answer (`stage: synchronous`), one
  when an asynchronous job ends (`executed`) and one per delivery of its result (`delivered`),
  with the receipt id, the cache origin and the mode, never a value, member or policy;
- **`policy.attribute_missing` / `policy.mapping_missing`** (P3-09, #443): one per policy that
  withheld every row, with the policy's dataset, its name and the receipt id.

The events are internal: nothing here reaches the caller's response. The one exception to
"no event, no answer" (Owner precision 5) is a **withheld** answer (a row policy withheld every
row, `Answer.withheld`): when its events cannot be recorded it is still returned, and each
failure is counted on the audit log (`AuditLog.unrecorded`) and written once to the process
log, naming only the kind and the project. Any other answer, zero rows included, whose `query`
event cannot be recorded is refused with `AUDIT_UNAVAILABLE`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from orvex.access.audit.log import AuditLog
from orvex.access.errors import AccessError
from orvex.semantic.execution import Answer

LOG = logging.getLogger(__name__)
KINDS = {
    "POLICY_ATTRIBUTE_MISSING": "policy.attribute_missing",
    "POLICY_MAPPING_MISSING": "policy.mapping_missing",
}


def record_query(
    audit: AuditLog,
    found: Answer,
    context: Mapping[str, Any],
    stage: str,
    job: str | None = None,
) -> None:
    """The `query` event of `stage`; `AUDIT_UNAVAILABLE` when it cannot be recorded, unless the
    answer is withheld (#443: counted and logged, the answer still returned)."""
    project = str(context["project"])
    document = found.receipt.document()
    details = {
        "receipt": found.receipt.receipt_id,
        "cache": str(document["execution"]["cache"]),
        "mode": found.plan.mode,
        "stage": stage,
    } | ({"job": job} if job is not None else {})
    try:
        audit.record_alone(
            "query", str(context["user"]), details, outcome="allowed", project=project
        )
    except AccessError:
        if not found.withheld:
            raise
        audit.unrecorded += 1
        LOG.error("audit: a query event of project %s could not be recorded", project)


def record_withheld(audit: AuditLog, found: Answer, context: Mapping[str, Any]) -> None:
    """Record, before the answer is returned, why each policy gave the caller no row."""
    project = str(context["project"])
    for held in found.withheld:
        kind = KINDS[held.code]
        details = {
            "dataset": held.dataset,
            "policy": held.policy,
            "receipt": found.receipt.receipt_id,
        }
        try:
            audit.record_alone(
                kind, str(context["user"]), details, outcome="refused", project=project
            )
        except AccessError:
            audit.unrecorded += 1
            LOG.error("audit: a %s event of project %s could not be recorded", kind, project)
