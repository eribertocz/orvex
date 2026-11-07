"""P2-22 (#344): every obligation is discharged by evidence a separate check finds in the plan
and in the statement, never by the planner's own claims; a plan or statement without it never
runs (C9)."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import cache
from typing import Any

import pytest
import yaml
from orvex.semantic import execution
from orvex.semantic.adapters import duckdb as adapter
from orvex.semantic.correctness import analyze
from orvex.semantic.correctness.analysis import RULES
from orvex.semantic.correctness.discharge import (
    CHECKERS,
    RULES_CHECKED,
    DischargeError,
    verify_physical,
    verify_plan,
)
from orvex.semantic.correctness.report import Obligation
from orvex.semantic.errors import SemanticError
from orvex.semantic.planning import Division, PlanError, SemanticPlan, plan
from orvex.semantic.policies import read_policies
from orvex.semantic.query.binding import Limits, bind_query

from conformance.runners import corpus_execution as ce
from tests.conformance.test_execution import Spy, ask, corpus_case, lake_for

Mutation = tuple[str, SemanticPlan]


def context(case: str) -> dict[str, Any]:
    return ce.viewer_context(attributes=ce.CALLERS.get(case, {}))


def policies(case: str) -> Any:  # noqa: ANN401
    compiled, _, model = corpus_case(case)
    return read_policies(
        yaml.safe_dump(ce.policies_of(model)), compiled, groups=frozenset(), managed_tables={}
    )


def planned(case: str) -> SemanticPlan | None:
    """The plan of a corpus case, made from scratch, or None for a case refused before it."""
    try:
        compiled, _, _ = corpus_case(case)
        bound = bind_query(
            compiled,
            json.dumps(ce.QUERIES[case]),
            instant=ce.INSTANT,
            limits=Limits(detail_page_size=1000),
            hidden=lambda _: False,
        )
        return plan(analyze(bound, compiled, policies(case), context(case)))
    except SemanticError:
        return None


@cache
def plans() -> dict[str, SemanticPlan]:
    found = {case: planned(case) for case in sorted(ce.QUERIES)}
    return {case: p for case, p in found.items() if p is not None}


def target(obligation: Obligation) -> str:
    return obligation.id.split(":", 1)[1]


def label(found: SemanticPlan, member: Any) -> str:  # noqa: ANN401
    text: str = found.compiled.require(member).parsed.label
    return text


def mutations(found: SemanticPlan, obligation: Obligation) -> list[Mutation]:
    """Each way to take away or bend the step that discharges `obligation`."""
    name = target(obligation)
    aggregate = next((a for a in found.aggregates if label(found, a.metric) == name), None)
    derived = next((d for d in found.derived if label(found, d.metric) == name), None)
    window = next((w for w in found.windows if label(found, w.metric) == name), None)
    others = tuple(a for a in found.aggregates if a is not aggregate)

    def changed(**fields: Any) -> tuple[Any, ...]:  # noqa: ANN401
        return tuple(replace(a, **fields) if a is aggregate else a for a in found.aggregates)

    kind = (obligation.rule, obligation.kind)
    if kind in (("C1", "aggregate"), ("C2", "distinct"), ("C3", "isolate"), ("C7", "allocate")):
        assert aggregate is not None
        out: list[Mutation] = [("drop the aggregation", replace(found, aggregates=others))]
        own = [g for g in found.groups if g.source == aggregate.source]
        if own:
            dropped = tuple(g for g in found.groups if g is not own[0])
            out.append(("drop a group of its fact", replace(found, groups=dropped)))
        joined = [j.right for j in found.joins if j.left == aggregate.source]
        if joined:
            moved = changed(source=joined[0])
            out.append(("aggregate outside its fact", replace(found, aggregates=moved)))
        if kind == ("C2", "distinct"):
            divided = changed(division=Division(2, "half_away_from_zero", "null"))
            out.append(("divide a distinct count", replace(found, aggregates=divided)))
        if kind == ("C3", "isolate"):
            out.append(("aggregate the facts together", replace(found, facts=found.facts[:1])))
        if kind == ("C7", "allocate"):
            out.append(("never round", replace(found, aggregates=changed(division=None))))
        return out
    if kind == ("C2", "recompute"):
        if derived is not None:
            assert derived.division is not None

            def bent(**fields: Any) -> SemanticPlan:  # noqa: ANN401
                steps = tuple(replace(d, **fields) if d is derived else d for d in found.derived)
                return replace(found, derived=steps)

            first, second = derived.over
            other = replace(derived.division, scale=derived.division.scale + 1)
            return [
                ("average a ratio", bent(division=None)),
                ("divide the denominator by the numerator", bent(over=(second, first))),
                ("divide a component by itself", bent(over=(first, first))),
                ("round another way", bent(division=other)),
            ]
        assert aggregate is not None
        assert aggregate.division is not None
        other = replace(aggregate.division, mode="half_even")
        return [
            ("average a ratio", replace(found, aggregates=changed(division=None))),
            ("round another way", replace(found, aggregates=changed(division=other))),
        ]
    if kind == ("C2", "period_end"):
        assert aggregate is not None
        sources = found.sources

        def at(**fields: Any) -> tuple[Any, ...]:  # noqa: ANN401
            return tuple(
                replace(s, **fields) if s.alias == aggregate.source else s for s in sources
            )

        fact = next(s for s in sources if s.alias == aggregate.source)
        assert fact.latest is not None
        out = [("sum every snapshot", replace(found, sources=at(latest=None)))]
        out.append(
            ("no entity", replace(found, sources=at(latest=replace(fact.latest, entity=()))))
        )
        unit = "year" if fact.latest.bucket != "year" else "month"
        yearly = replace(fact.latest, bucket=unit)
        out.append(("another period", replace(found, sources=at(latest=yearly))))
        per_day = replace(fact.latest, entity=(fact.latest.axis,))
        out.append(("rank per day, not per entity", replace(found, sources=at(latest=per_day))))
        zoned = replace(fact.latest, zone="America/La_Paz")
        out.append(("cut the period in another zone", replace(found, sources=at(latest=zoned))))
        return out
    if kind == ("C5", "align"):
        assert derived is not None
        rest = tuple(d for d in found.derived if d is not derived)
        unknown = tuple(replace(d, over=("#none",)) if d is derived else d for d in found.derived)
        return [
            ("drop the metric over metrics", replace(found, derived=rest)),
            ("read a value no aggregation gives", replace(found, derived=unknown)),
        ]
    if kind == ("C6", "as_of"):

        def guarded(**fields: Any) -> SemanticPlan:  # noqa: ANN401
            joins = tuple(
                replace(j, as_of=replace(j.as_of, **fields)) if j.as_of else j for j in found.joins
            )
            return replace(found, joins=joins)

        (temporal,) = [j.as_of for j in found.joins if j.as_of]
        joins = tuple(replace(j, as_of=None) for j in found.joins)
        return [
            ("join without the as-of guard", replace(found, joins=joins)),
            (
                "swap the validity bounds",
                guarded(valid_from=temporal.valid_to, valid_to=temporal.valid_from),
            ),
            ("guard no rows", guarded(rows=())),
        ]
    if kind == ("C7", "weights"):

        def allocated(**fields: Any) -> SemanticPlan:  # noqa: ANN401
            steps = tuple(
                replace(a, allocation=replace(a.allocation, **fields) if fields else None)
                if a.allocation and a.allocation.bridge == name
                else a
                for a in found.aggregates
            )
            return replace(found, aggregates=steps)

        return [
            ("allocate without checking weights", allocated()),
            ("normalize over nothing", allocated(scope=())),
            ("another tolerance", allocated(tolerance="0.5")),
            ("another total", allocated(total="2")),
        ]
    if kind == ("C9", "policy"):
        open_ = tuple(replace(s, policies=()) for s in found.sources)
        placement = found.placement
        bent_rules = {
            d: tuple(replace(r, rule=replace(r.rule, orphans="visible")) for r in rs)
            for d, rs in placement.restrictions.items()
        }
        other_path = {
            d: tuple(replace(r, path=("elsewhere",)) for r in rs)
            for d, rs in placement.restrictions.items()
        }
        return [
            ("read without the row policy", replace(found, sources=open_)),
            (
                "show orphans the policy hides",
                replace(found, placement=replace(placement, restrictions=bent_rules)),
            ),
            (
                "restrict along another path",
                replace(found, placement=replace(placement, restrictions=other_path)),
            ),
        ]
    if kind == ("S9", "window"):
        assert window is not None
        rest_w = tuple(w for w in found.windows if w is not window)
        unordered = tuple(replace(w, order=()) if w is window else w for w in found.windows)
        return [
            ("drop the running total", replace(found, windows=rest_w)),
            ("no order", replace(found, windows=unordered)),
        ]
    raise AssertionError(f"no mutation for {kind}")


def cases() -> list[tuple[str, str, str]]:
    found = []
    for case, p in plans().items():
        for obligation in p.report.obligations:
            for name, _ in mutations(p, obligation):
                found.append((case, obligation.id, name))
    return found


# --- evidence ---------------------------------------------------------------------------------


def test_every_obligation_of_every_answered_case_has_its_evidence() -> None:
    assert len(plans()) == 18
    for case, found in plans().items():
        assert set(found.evidence) == {o.id for o in found.report.obligations}, case
        for obligation in found.report.obligations:
            step, _, element = found.evidence[obligation.id].partition(":")
            assert step in {
                "aggregate",
                "latest",
                "derived",
                "window",
                "source",
                "weights",
                "as_of",
            }
            assert element, case
            assert element not in {s.alias for s in found.sources}, case  # a model name


def test_evidence_is_deterministic() -> None:
    for case, found in plans().items():
        again = planned(case)
        assert again is not None
        assert (again.evidence, again.fingerprint) == (found.evidence, found.fingerprint), case


def test_every_rule_the_engine_applies_has_a_checker() -> None:
    assert {rule for rule, _ in RULES} == RULES_CHECKED
    used = {(o.rule, o.kind) for p in plans().values() for o in p.report.obligations}
    # The corpus exercises every checker but the detail page's (`test_detail_rows`) and the
    # aggregate table's (`test_materialization`): no normative case declares what a table stores.
    assert used | {("C1", "rows"), ("C8", "equivalent")} == set(CHECKERS)


def test_the_receipt_names_the_evidence_and_hides_it_from_who_cannot_see_its_datasets() -> None:
    compiled, _, _ = corpus_case("CASE-015")
    with lake_for("CASE-015") as lake:
        found = ask(
            ce.QUERIES["CASE-015"],
            compiled,
            lake,
            policy=policies("CASE-015"),
            context=context("CASE-015"),
        )
    recorded = {
        o["obligation"]: o["evidence"] for o in found.receipt.document()["plan"]["obligations"]
    }
    assert recorded == {
        "C1.aggregate:revenue": "aggregate:sales",
        "C9.policy:sales": "source:sales",
    }

    class Builder:
        builder = True

        def __init__(self, sees: bool) -> None:
            self.sees = sees

        def sees_member(self, member: str) -> bool:
            return True

        def sees_value(self, member: str, value: object) -> bool:
            return True

        def sees_dataset(self, dataset: str) -> bool:
            return self.sees

        def knows_policy(self, name: str) -> bool:
            return True

    shown = found.receipt.view(Builder(True))["plan"]["obligations"]
    assert all("evidence" in o for o in shown)
    hidden = found.receipt.view(Builder(False))["plan"]["obligations"]
    assert all("evidence" not in o for o in hidden)


# --- failing plans ----------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "obligation", "mutation"), cases())
def test_a_plan_without_the_evidence_of_an_obligation_never_runs(
    case: str, obligation: str, mutation: str
) -> None:
    found = plans()[case]
    wanted = next(o for o in found.report.obligations if o.id == obligation)
    mutated = dict(mutations(found, wanted))[mutation]
    with pytest.raises(DischargeError):
        verify_plan(mutated)


def test_every_obligation_kind_has_a_failing_plan() -> None:
    kinds = set()
    for case, obligation, _ in cases():
        found = plans()[case]
        kinds |= {(o.rule, o.kind) for o in found.report.obligations if o.id == obligation}
    assert kinds | {("C1", "rows"), ("C8", "equivalent")} == set(CHECKERS)  # tested apart


def test_a_step_s_claim_alone_discharges_nothing() -> None:
    found = plans()["CASE-009"]
    (derived,) = found.derived
    claimed = tuple(
        replace(a, discharges=(*a.discharges, *derived.discharges)) for a in found.aggregates
    )
    with pytest.raises(DischargeError):
        verify_plan(replace(found, aggregates=claimed, derived=()))


def test_a_policy_dropped_from_the_placement_is_still_caught() -> None:
    found = plans()["CASE-015"]
    empty = replace(found.placement, restrictions={})
    open_ = tuple(replace(s, policies=()) for s in found.sources)
    with pytest.raises(DischargeError):  # the policy model still reaches the dataset
        verify_plan(replace(found, sources=open_, placement=empty))


# --- failing statements -----------------------------------------------------------------------


def physical(found: SemanticPlan) -> Any:  # noqa: ANN401
    return adapter.compile_plan(found)


def test_every_answered_case_s_statement_holds_its_evidence() -> None:
    for found in plans().values():
        verify_physical(found, physical(found))


@pytest.mark.parametrize(
    ("case", "change"),
    [
        ("CASE-013", {"as_of": ()}),
        ("CASE-011", {"weights": ()}),
        ("CASE-013", {"sql": "drop the guard column"}),
        ("CASE-015", {"sql": "read the bare table"}),
        ("CASE-015", {"sql": "add a bare read named like a chain"}),
        ("CASE-015", {"sql": "restrict by nothing"}),
        ("CASE-015", {"sql": "read it qualified"}),
        ("CASE-015", {"sql": "read it through the catalog"}),  # `lake.sales` is `main.sales`
        ("CASE-015", {"sql": "read it in another case"}),
        ("CASE-015", {"sql": "read it fully qualified"}),
        ("CASE-015", {"sql": "unbalance the parentheses"}),
    ],
)
def test_a_statement_without_its_evidence_is_refused_before_any_read(
    case: str, change: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    found = plans()[case]
    written = physical(found)
    if change.get("sql") == "drop the guard column":
        (column, _), *_ = written.as_of
        change = {"sql": written.sql.replace(f'"{column}"', '"#other"')}
    elif change.get("sql") == "read the bare table":
        bare = adapter._Writer(replace(found, placement=None)).statement()
        change = {"sql": bare.sql}
    elif change.get("sql") == "add a bare read named like a chain":
        change = {"sql": f'{written.sql} UNION ALL SELECT * FROM "sales" AS "#c1"'}
    elif change.get("sql") == "restrict by nothing":
        start = written.sql.index('(SELECT * FROM "sales" AS "#p" WHERE ')
        depth, end = 0, start
        while True:
            depth += {"(": 1, ")": -1}.get(written.sql[end], 0)
            if depth == 0:
                break
            end += 1
        whole = written.sql[start : end + 1]
        change = {"sql": written.sql.replace(whole, '(SELECT * FROM "sales" AS "#p" WHERE TRUE)')}
    elif change.get("sql") == "read it qualified":
        change = {"sql": f'{written.sql} UNION ALL SELECT * FROM "main"."sales" AS "x"'}
    elif change.get("sql") == "read it through the catalog":
        change = {"sql": f'{written.sql} UNION ALL SELECT * FROM "lake"."sales" AS "x"'}
    elif change.get("sql") == "read it in another case":
        change = {"sql": f'{written.sql} UNION ALL SELECT * FROM "Main"."SALES" AS "x"'}
    elif change.get("sql") == "read it fully qualified":
        change = {"sql": f'{written.sql} UNION ALL SELECT * FROM "lake"."main"."sales" AS "x"'}
    elif change.get("sql") == "unbalance the parentheses":
        change = {"sql": written.sql + ")"}
    broken = replace(written, **change)
    with pytest.raises(DischargeError):
        verify_physical(found, broken)
    compiled, _, _ = corpus_case(case)
    spy = Spy()
    monkeypatch.setattr(execution, "compile_plan", lambda _: broken)
    with lake_for(case) as lake, pytest.raises(PlanError):
        ask(
            ce.QUERIES[case], compiled, lake, policy=policies(case), context=context(case), read=spy
        )
    assert spy.calls == 0


def test_a_forged_plan_given_to_execution_never_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a plan that claims to be issued is checked again before the statement runs."""
    found = plans()["CASE-015"]
    open_ = tuple(replace(s, policies=()) for s in found.sources)
    forged = replace(found, sources=open_)
    object.__setattr__(forged, "issued", found.issued)
    monkeypatch.setattr(execution, "plan", lambda *_: forged)
    compiled, _, _ = corpus_case("CASE-015")
    spy = Spy()
    with lake_for("CASE-015") as lake, pytest.raises(DischargeError):
        ask(
            ce.QUERIES["CASE-015"],
            compiled,
            lake,
            policy=policies("CASE-015"),
            context=context("CASE-015"),
            read=spy,
        )
    assert spy.calls == 0
