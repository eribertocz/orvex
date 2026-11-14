"""The aggregate counterpart of a security case whose engine does not serve detail pages yet
(#447, the Owner's condition for `pending_engines`
and for a case `pending` on detail pages): the same attack, end to end, on a query
that engine supports, so the report tells a protection shown from one only refused.

A case's detail query (`mode: detail`) becomes an aggregate one: each field with a metric of
its own (`METRIC_OF`) is asked as that metric, every other field is grouped by, and the order is
kept. Grouped by the dataset's primary key, each group is one row, so the expected rows are the
case's own with those columns renamed; its codes, its forbidden values and its control stay as
they are. A case is left without a counterpart when its fields cannot be grouped that way.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from conformance.runners import security_fixtures as sf

# The metric that answers a field of one row exactly, by fixture member (`SUM` over one row).
METRIC_OF = {"outages.minutes": "outage_minutes"}


def counterpart(case: Mapping[str, Any], found: sf.Fixture) -> dict[str, Any] | None:
    """The case asked in aggregate mode, or None when it has no such counterpart."""
    datasets: Any = found.compiled.ossie.document.get("datasets", ())
    keys = {f"{d['name']}.{k}" for d in datasets for k in d.get("primary_key", ())}
    made: dict[str, Any] = copy.deepcopy(dict(case))
    bodies = {id(b): b for b in _bodies(made["request"]) + _bodies(made["control"].get("request"))}
    detail = [b for b in bodies.values() if b.get("mode") == "detail"]
    if not detail:
        return None
    for body in detail:
        fields = [f["member"] for f in body["fields"]]
        grouped = [m for m in fields if m not in METRIC_OF]
        if not keys & set(grouped) or not set(fields) & set(METRIC_OF):
            return None
        order = [o for o in body.get("order", ()) if o["by"] in grouped]
        body.clear()
        body.update(
            {
                "kind": "semantic_query",
                "version": 1,
                "mode": "aggregate",
                "metrics": [{"metric": METRIC_OF[m]} for m in fields if m in METRIC_OF],
                "group_by": [{"member": m} for m in grouped],
                "order": order,
            }
        )
    for expectation in (made["expect"], made["control"]):
        if "rows" in expectation:
            head, _, rest = str(expectation["rows"]).partition("\n")
            names = [METRIC_OF.get(c, c) for c in head.split(",")]
            expectation["rows"] = ",".join(names) + "\n" + rest
    made.pop("pending_engines", None)
    made.pop("pending", None)
    return made


def _bodies(request: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The query bodies of a request or a sequence (YAML anchors may share one)."""
    if request is None:
        return []
    steps = request["steps"] if request["kind"] == "sequence" else [{"request": request}]
    return [s["request"]["body"] for s in steps if s["request"]["kind"] == "semantic-query"]
