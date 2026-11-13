"""The audit events of a semantic answer (P3-09, #443): in server mode, one event per policy
that withheld every row from the caller, `policy.attribute_missing` or `policy.mapping_missing`,
with the policy's dataset, its name and the answer's receipt id, and never a value.

The events are internal: nothing here reaches the caller's response. When one cannot be
recorded, the answer is still returned (it withholds every row anyway), and the failure is
counted on the audit log (`AuditLog.unrecorded`) and written once to the process log, naming
only the kind and the project: never a user, dataset, policy, attribute or value.
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
