"""Row policies on the existing DuckLake, through the questions it does serve (#441, Owner:
its detail pages wait for P5-14): sums, counts, distinct counts, groups and totals, for every
kind of caller of the `direct-query` fixture, on each catalog. Each answer is the managed
lake's for the same caller and the value computed here from the fixture's rows; the mapping
tables stay in the managed lake (ADR-0027 §6).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_security_scenarios import SEPARATOR
from tests.support.security_engines import ask, served

FOUND = sf.fixture("direct-query", ce.VENDORS[0])
# id, zone, minutes: never null in this fixture
OUTAGES: list[list[str]] = [[str(v) for v in r] for r in FOUND.rows["outages"]]


def caller(user: str, groups: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "kind": "security_context",
        "version": 1,
        "user": user,
        "project": "direct-query",
        "role": "author",
        "groups": list(groups),
        "attributes": {},
        "permission_fingerprint": "sha256:" + "0" * 64,
    }


def allowed(user: str, exempt: bool = False) -> set[str]:
    """Zones `user` may read: mapped (allow-list, unless `exempt` from it), registered, and not
    blocked (deny-list, whose NULL means no row under SQL's NOT IN)."""
    rows = FOUND.rows
    every = {r[1] for r in OUTAGES}
    mapped = every if exempt else {z for u, z in rows["security.user_zone"] if u == user}
    registered = any(u == user for (u,) in rows["security.registered"])
    blocked = [z for u, z in rows["security.blocked_zone"] if u == user]
    if not registered or None in blocked:
        return set()
    return {str(z) for z in mapped} - {str(z) for z in blocked}


CAP = 2  # `limits.policy_values` of the fixture's security.yaml
USERS = ["u-zone1", "u-wide", "u-deny", "u-open", "u-denynull", "u-none", "u-unreg"]
# Every user as an author, then as exempt from the allow-list (`operations`).
CALLERS = [(u, ()) for u in USERS] + [(u, ("operations",)) for u in USERS]
QUESTIONS: dict[str, dict[str, Any]] = {
    "sum": {"metrics": [{"metric": "outage_minutes"}]},
    "count": {"metrics": [{"metric": "outage_count"}]},
    "distinct": {"metrics": [{"metric": "zone_count"}]},
    "grouped": {
        "metrics": [{"metric": "outage_minutes"}, {"metric": "outage_count"}],
        "group_by": [{"member": "outages.zone_id"}],
        "order": [{"by": "outages.zone_id"}],
        "totals": "grand",
    },
}


def expected(question: str, zones: set[str]) -> list[list[str | None]]:
    rows = [r for r in OUTAGES if r[1] in zones]
    minutes = f"{sum(Decimal(r[2]) for r in rows):.2f}" if rows else None
    if question == "sum":
        return [[minutes]]
    if question == "count":
        return [[str(len(rows))]]
    if question == "distinct":
        return [[str(len({r[1] for r in rows}))]]
    by_zone: list[list[str | None]] = [
        [r[1], f"{Decimal(r[2]):.2f}", "1"] for r in sorted(rows, key=lambda r: r[1])
    ]
    return [*by_zone, [None, minutes, str(len(rows))]]


@pytest.mark.parametrize("catalog", ["sqlite", "postgres"])
def test_the_existing_lake_answers_every_supported_question_as_the_policy_allows(
    catalog: str,
) -> None:
    with (
        served(FOUND, "managed", "") as managed,
        served(FOUND, "existing-ducklake", catalog) as other,
    ):
        for user, groups in CALLERS:
            for name, body in QUESTIONS.items():
                request = {
                    "body": {"kind": "semantic_query", "version": 1, "mode": "aggregate", **body}
                }
                on_managed = ask(FOUND, managed, request, caller(user, groups))
                on_other = ask(FOUND, other, request, caller(user, groups))
                label = (user, name)
                zones = allowed(user, exempt=bool(groups))
                assert on_managed.rows == expected(name, zones), label
                mapped = {z for u, z in FOUND.rows["security.user_zone"] if u == user}
                if not groups and len(mapped) > CAP:
                    # SEC-DQ-004: more mapped values than the engine binds; refused, no size
                    assert on_other.code == "POLICY_NOT_SUPPORTED_ON_ENGINE", label
                    assert not {"Z1", "Z2", "Z3", "3", "2"} & {  # no value, no size (SEC-DQ-004)
                        t for text in on_other.texts for t in SEPARATOR.split(text)
                    }, label
                    continue
                assert on_other.code is None, (label, on_other)
                assert on_other.rows == on_managed.rows, label
                assert on_other.notices == on_managed.notices, label  # law 4
                assert on_other.rows == expected(name, zones), label
        assert other.reads  # the existing lake did answer, read by read


def test_the_callers_cover_every_outcome() -> None:
    """The callers above see none, some and every zone: the test is not vacuous."""
    seen = {frozenset(allowed(u, bool(g))) for u, g in CALLERS}
    assert {
        frozenset(),
        frozenset({"Z1"}),
        frozenset({"Z1", "Z2"}),
        frozenset({"Z1", "Z2", "Z3"}),
    } <= seen
