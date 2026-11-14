"""The result cache (#444 AC2 to AC6, AC8, AC8b): a hit only when the execution identity of this
request is the stored one, rebuilt for the request (a cursor and a sealed receipt of its own);
a miss for every change that could change the answer; never a refusal or an error stored;
bounded; keyed by exactly the snapshot the request reads, whatever commits land meanwhile.

On the `utility` fixture, with a caller restricted to Z1 (`u-zone1`) and an exempt one
(`u-exempt`); each caller's context is built anew per request, as the server builds it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.access.context import permission_fingerprint
from orvex.contracts.store.v1 import StatementRequest
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.receipt import verify_receipt
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.semantic.result_cache import ResultCache
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_detail_rows import FORM
from tests.support.security_sequences import compile_with

FOUND = sf.fixture("utility", ce.VENDORS[0])
UTILITY = yaml.safe_load((FOUND.folder / "security.yaml").read_text("utf-8"))
BY_ZONE = {"kind": "semantic_query", "version": 1, "mode": "aggregate",
           "metrics": [{"metric": "total_kwh"}], "group_by": [{"member": "readings.zone_id"}],
           "order": [{"by": "readings.zone_id", "direction": "asc"}]}  # fmt: skip
DETAIL = {"kind": "semantic_query", "version": 1, "mode": "detail",
          "fields": [{"member": "readings.reading_id"}, {"member": "readings.kwh"}],
          "order": [{"by": "readings.reading_id", "direction": "asc"}],
          "page": {"size": "1"}}  # fmt: skip


def policies(document: Mapping[str, Any] = UTILITY) -> PolicyModel:
    return read_policies(yaml.safe_dump(dict(document)), FOUND.compiled, groups=sf.GROUPS,
                         managed_tables=FOUND.managed)  # fmt: skip


def caller(
    lake: Lake,
    model: PolicyModel,
    user: str = "u-zone1",
    groups: tuple[str, ...] = (),
    **attributes: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """The caller's context for this request, from the state now (as `security_sequences`)."""
    snapshot = str(lake.current_snapshot().id)
    typed = {k: {"type": "text", "value": v} for k, v in attributes.items()}
    fingerprint = permission_fingerprint(
        user=user, project="utility", role="author", groups=sorted(groups), attributes=typed,
        policy_fingerprint=model.fingerprint,
        mapping_snapshots=dict.fromkeys(sorted(model.managed_reads), snapshot),
    )  # fmt: skip
    return {"kind": "security_context", "version": 1, "user": user, "project": "utility",
            "role": "author", "groups": sorted(groups), "attributes": typed,
            "permission_fingerprint": fingerprint}  # fmt: skip


class Counted:
    """The lake's reads, counted."""

    def __init__(self, lake: Lake) -> None:
        self.lake, self.calls = lake, 0

    def __call__(self, request: Any) -> Any:  # noqa: ANN401
        self.calls += 1
        return self.lake.read(request)


@contextmanager
def lake() -> Iterator[Lake]:
    with sf.managed_lake(FOUND) as opened:
        yield opened


def engine(opened: Lake, cache: ResultCache | None, **more: Any) -> Any:  # noqa: ANN401
    snapshot = opened.current_snapshot()
    pinned = {"id": snapshot.id, "committed_at": snapshot.committed_at.isoformat()}

    def kept(version: str) -> Mapping[str, object] | None:
        return pinned if version == str(snapshot.id) else None

    return replace(ce.engine_for(opened, cursors=FORM, pinned=kept, **more), cache=cache)


def ask(
    opened: Lake,
    cache: ResultCache | None,
    document: Mapping[str, Any],
    context: Mapping[str, Any],
    model: PolicyModel,
    compiled: Any = None,  # noqa: ANN401
    **more: Any,  # noqa: ANN401
) -> Answer:
    return answer(json.dumps(document), compiled or FOUND.compiled, model, dict(context),
                  engine(opened, cache, **more))  # fmt: skip


def origin(found: Answer) -> str:
    return str(found.receipt.document()["execution"]["cache"])


def same(first: Answer, second: Answer) -> None:
    """The same answer, cache origin and per-request fields apart."""
    assert ce.rows_of(first) == ce.rows_of(second)
    a, b = first.result.envelope(), second.result.envelope()
    for envelope in (a, b):
        envelope.pop("receipt", None)
        envelope.get("page", {}).pop("next", None)
    assert a == b
    assert (
        first.receipt.document()["execution_fingerprint"]
        == (second.receipt.document()["execution_fingerprint"])
    )


def commit(opened: Lake, sql: str) -> None:
    with opened.transaction() as transaction:
        transaction.execute(StatementRequest(kind="statement", parameters=[], sql=sql))


# --- AC3: a hit, rebuilt for its request ------------------------------------------------------


def test_a_hit_reads_nothing_and_is_rebuilt_for_its_request() -> None:
    model, cache = policies(), ResultCache()
    with lake() as opened:
        context = caller(opened, model)
        first = ask(opened, cache, DETAIL, context, model)
        spy = Counted(opened)
        second = ask(opened, cache, DETAIL, caller(opened, model), model, read=spy)
        assert spy.calls == 0  # the hit read nothing: not the data, not a zone or a mapping
        assert (origin(first), origin(second)) == ("miss", "hit")
        same(first, second)
        assert second.receipt.receipt_id != first.receipt.receipt_id  # its own record
        viewer = CallerViewer(FOUND.compiled, model, context)
        view = second.receipt.view(viewer)
        assert view["cache"] == "hit"
        assert verify_receipt(view, second.receipt, ce.SEAL_KEY, viewer)
        token = second.result.envelope()["page"]["next"]
        assert token != first.result.envelope()["page"]["next"]  # a cursor of its own
        uncached = ask(opened, None, {**DETAIL, "page": {"size": "1", "after": token}}, context,
                       model)  # fmt: skip
        after = first.result.envelope()["page"]["next"]
        reference = ask(opened, None, {**DETAIL, "page": {"size": "1", "after": after}}, context,
                        model)  # fmt: skip
        assert ce.rows_of(uncached) == ce.rows_of(reference)
        # After a revocation (the caller's mapping row deleted), that cursor is refused, as one
        # from a miss would be.
        commit(opened, "DELETE FROM security.user_zone WHERE user_id = 'u-zone1'")
        with pytest.raises(SemanticError) as caught:
            ask(opened, cache, {**DETAIL, "page": {"size": "1", "after": token}},
                caller(opened, model), model)  # fmt: skip
        assert caught.value.code == "PAGE_CURSOR_INVALID"


# --- AC2: a miss for every change -------------------------------------------------------------


def _edited() -> Any:  # noqa: ANN401
    def doubled(model: dict[str, Any]) -> None:
        for metric in model["metrics"]:
            if metric["name"] == "total_kwh":
                metric["expression"] = {"sql": "SUM(readings.kwh * 2)"}

    return compile_with(FOUND, doubled)


MAPPED = "INSERT INTO security.user_zone VALUES ('u-zone1', 'Z2')"
UNMAPPED = "DELETE FROM security.user_zone WHERE user_id = 'u-zone1'"
FEWER_ROWS = {**UTILITY, "row_policies": UTILITY["row_policies"][:1]}
CHANGES: dict[str, Any] = {
    "another caller": lambda o, m: {"context": caller(o, m, "u-multi", ("management",))},
    "a lost group": lambda o, m: {"context": caller(o, m, "u-exempt")},
    "an attribute": lambda o, m: {
        "context": caller(o, m, "u-exempt", ("management",), region="north")
    },
    "a mapping row": lambda o, m: commit(o, MAPPED),
    "a removed mapping row": lambda o, m: commit(o, UNMAPPED),
    "a policy": lambda o, m: {"model": policies(FEWER_ROWS)},
    "a column policy": lambda o, m: {"model": policies({**UTILITY, "column_policies": []})},
    "an edited metric": lambda o, m: {"compiled": _edited()},
    "an engine version": lambda o, m: {"engine_version": "0.0.0-other"},
    "deleted rows": lambda o, m: commit(o, "DELETE FROM marts.readings WHERE reading_id = 1"),
    "an unrelated commit": lambda o, m: commit(o, "CREATE TABLE unrelated (x INTEGER)"),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_every_change_misses_and_answers_as_without_the_cache(change: str) -> None:
    model, cache = policies(), ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        first = ask(opened, cache, BY_ZONE, context, model)
        made = CHANGES[change](opened, model) or {}
        now = made.pop("model", model)
        if "context" not in made:  # rebuilt from the state now, as every request is
            made["context"] = caller(opened, now, "u-exempt", ("management",))
        asked = made.pop("context")
        compiled = made.pop("compiled", None)
        try:
            cached = ask(opened, cache, BY_ZONE, asked, now, compiled, **made)
        except SemanticError as error:
            refused = (error.code, dict(error.details))
            with pytest.raises(SemanticError) as without:
                ask(opened, None, BY_ZONE, asked, now, compiled, **made)
            assert refused == (without.value.code, dict(without.value.details))
            return
        assert origin(cached) == "miss", change
        reference = ask(opened, None, BY_ZONE, asked, now, compiled, **made)
        same(cached, reference)
        assert (
            first.receipt.document()["execution_fingerprint"]
            != (cached.receipt.document()["execution_fingerprint"])
        )  # another identity, never the stored entry


# --- AC4: differential property ---------------------------------------------------------------

STEP = st.tuples(
    st.sampled_from(["u-zone1", "u-exempt", "u-multi"]),
    st.sampled_from([(), ("management",)]),
    st.sampled_from([BY_ZONE, DETAIL]),
    st.sampled_from([None, None, None, "mapping", "unmapping", "data", "unrelated", "policy",
                     "attribute", "engine"]),
)  # fmt: skip
# Each step is asked twice in a row, so every sequence holds questions a cache may answer.
STEPS = st.lists(STEP, min_size=2, max_size=5).map(
    lambda s: [x for step in s for x in (step, (*step[:3], None))]
)
COMMITS = {
    "mapping": "INSERT INTO security.user_zone VALUES ('u-zone1', 'Z3')",
    "unmapping": "DELETE FROM security.user_zone WHERE user_id = 'u-multi'",
    "data": "DELETE FROM marts.readings WHERE reading_id = "
    "(SELECT max(reading_id) FROM marts.readings)",
    "unrelated": "CREATE TABLE IF NOT EXISTS unrelated (x INTEGER)",
}


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=STEPS)
def test_with_or_without_the_cache_every_answer_is_the_same(steps: list[Any]) -> None:
    model, cache = policies(), ResultCache()
    attributes: dict[str, str] = {}
    more: dict[str, Any] = {}
    hits = 0
    with lake() as opened:
        for user, groups, document, change in steps:
            if change == "policy":
                model = policies(FEWER_ROWS)
            elif change == "attribute":
                attributes = {"region": "north"}
            elif change == "engine":
                more = {"engine_version": "0.0.0-other"}
            elif change is not None:
                commit(opened, COMMITS[change])
            context = caller(opened, model, user, groups, **attributes)
            try:
                cached = ask(opened, cache, document, context, model, **more)
            except SemanticError as error:
                code = error.code
                with pytest.raises(SemanticError) as without:
                    ask(opened, None, document, context, model)
                assert code == without.value.code
                continue
            hits += origin(cached) == "hit"
            same(cached, ask(opened, None, document, context, model, **more))
            viewer = CallerViewer(FOUND.compiled, model, context)
            assert verify_receipt(cached.receipt.view(viewer), cached.receipt, ce.SEAL_KEY, viewer)
    assert hits >= 1  # every sequence repeats a question with nothing changed


# --- AC5, AC6: bypass, expiry, refusals ----------------------------------------------------------


def _with_cache(setting: Mapping[str, Any]) -> Any:  # noqa: ANN401
    return compile_with(FOUND, lambda m: [d.setdefault("custom_extensions", []).append(
        {"vendor_name": ce.VENDORS[0], "data": json.dumps(
            {"version": 1, "element": "dataset", "cache": dict(setting)})})
        for d in m["datasets"] if d["name"] == "readings"])  # fmt: skip


def test_a_dataset_with_the_cache_off_is_bypassed_and_others_still_cached() -> None:
    compiled = _with_cache({"enabled": False})
    model = read_policies(yaml.safe_dump(UTILITY), compiled, groups=sf.GROUPS,
                          managed_tables=FOUND.managed)  # fmt: skip
    cache = ResultCache()
    other = {"kind": "semantic_query", "version": 1, "mode": "aggregate",
             "metrics": [{"metric": "tariff_count"}]}  # fmt: skip
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        for _ in range(2):
            assert origin(ask(opened, cache, BY_ZONE, context, model, compiled)) == "bypass"
        assert [origin(ask(opened, cache, other, context, model, compiled)) for _ in range(2)] == [
            "miss", "hit"]  # fmt: skip
    assert len(cache) == 1


def test_an_entry_older_than_its_ttl_misses() -> None:
    compiled = _with_cache({"enabled": True, "ttl": "PT10S"})
    model = read_policies(yaml.safe_dump(UTILITY), compiled, groups=sf.GROUPS,
                          managed_tables=FOUND.managed)  # fmt: skip
    now = [0.0]
    cache = ResultCache(clock=lambda: now[0])
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        seen = [origin(ask(opened, cache, BY_ZONE, context, model, compiled))]
        now[0] = 9.0
        seen.append(origin(ask(opened, cache, BY_ZONE, context, model, compiled)))
        now[0] = 10.0
        seen.append(origin(ask(opened, cache, BY_ZONE, context, model, compiled)))
    assert seen == ["miss", "hit", "miss"]


def test_a_ttl_of_zero_bypasses_the_cache() -> None:
    compiled = _with_cache({"enabled": True, "ttl": "PT0S"})
    model = read_policies(yaml.safe_dump(UTILITY), compiled, groups=sf.GROUPS,
                          managed_tables=FOUND.managed)  # fmt: skip
    cache = ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        seen = [origin(ask(opened, cache, BY_ZONE, context, model, compiled)) for _ in range(2)]
    assert seen == ["bypass", "bypass"]
    assert len(cache) == 0


def test_refusals_and_errors_are_never_stored() -> None:
    model, cache = policies(), ResultCache()
    refused = {**BY_ZONE, "group_by": [{"member": "consumers.national_id"}],
               "order": [{"by": "consumers.national_id", "direction": "asc"}]}  # fmt: skip
    with lake() as opened:
        coll = caller(opened, model, "u-exempt", ("collections", "management"))
        assert origin(ask(opened, cache, refused, coll, model)) == "miss"  # cached for them
        for _ in range(2):  # the same question, the group lost: refused, every time
            with pytest.raises(SemanticError) as caught:
                ask(opened, cache, refused, caller(opened, model, "u-exempt", ("management",)),
                    model)  # fmt: skip
            assert caught.value.code == "MEMBER_NOT_FOUND"
    assert len(cache) == 1


# --- AC8: bounds and concurrency ------------------------------------------------------------


def test_the_cache_holds_no_more_than_its_bound_and_skips_an_oversize_answer() -> None:
    model = policies()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        cache = ResultCache(max_bytes=16 * 1300)  # room for a few one-page answers only
        pages = [{**DETAIL, "page": {"size": str(size)}} for size in range(1, 21)]
        sizes = []
        for page in pages * 2:  # more distinct answers than fit: the oldest are evicted
            ask(opened, cache, page, context, model)
            sizes.append(cache.size_bytes)
            assert cache.size_bytes <= cache.max_bytes
        assert len(cache) < len(pages)  # some were evicted
        recent = origin(ask(opened, cache, pages[-1], context, model))
        oldest = origin(ask(opened, cache, pages[0], context, model))
        assert (recent, oldest) == ("hit", "miss")  # the least recently used went first
        tiny = ResultCache(max_bytes=16 * 1024)  # a sixteenth: 1 KiB, less than any entry
        ask(opened, tiny, BY_ZONE, context, model)
        assert len(tiny) == 0
        assert max(sizes) <= cache.max_bytes


def test_a_read_that_fails_is_never_stored() -> None:
    model, cache = policies(), ResultCache()

    def failing(request: Any) -> Any:  # noqa: ANN401
        raise StoreError("STORE_QUERY_FAILED", "simulated", {})

    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        with pytest.raises(StoreError):
            ask(opened, cache, BY_ZONE, context, model, read=failing)
        assert len(cache) == 0
        assert origin(ask(opened, cache, BY_ZONE, context, model)) == "miss"


def test_parallel_identical_questions_give_equal_answers() -> None:
    model, cache = policies(), ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        found: list[Answer] = []
        errors: list[BaseException] = []

        def one() -> None:
            for _ in range(50):  # the lake's own queue may be full: its answer, try again
                try:
                    found.append(ask(opened, cache, BY_ZONE, context, model))
                    return
                except StoreError as error:
                    if error.code != "STORE_BUSY":
                        errors.append(error)
                        return
                    time.sleep(0.05)
                except BaseException as error:  # noqa: BLE001 - reported below
                    errors.append(error)
                    return

        threads = [threading.Thread(target=one) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(found) == 8
        for each in found[1:]:
            same(found[0], each)


# --- AC8b: exactly the version read -----------------------------------------------------------


def test_a_commit_between_pinning_and_reading_never_crosses_snapshots() -> None:
    """The request pins S; a commit lands (S'); it reads and stores at S. A request at S'
    misses and sees the change; one still pinned at S hits its own snapshot's answer."""
    model, cache = policies(), ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        pinned = engine(opened, cache)
        commit(opened, "DELETE FROM marts.readings WHERE reading_id = 1")
        at_s = answer(json.dumps(BY_ZONE), FOUND.compiled, model, context, pinned)
        assert at_s.receipt.document()["data"][0]["snapshot_id"] == pinned.snapshot["id"]
        uncached = answer(json.dumps(BY_ZONE), FOUND.compiled, model, context,
                          replace(pinned, cache=None))  # fmt: skip
        assert ce.rows_of(at_s) == ce.rows_of(uncached)
        later = ask(opened, cache, BY_ZONE, caller(opened, model, "u-exempt", ("management",)),
                    model)  # fmt: skip
        assert origin(later) == "miss"
        assert ce.rows_of(later) != ce.rows_of(at_s)
        again = answer(json.dumps(BY_ZONE), FOUND.compiled, model, context, pinned)
        assert origin(again) == "hit"
        same(again, at_s)


def test_a_commit_during_the_read_stores_only_what_was_read() -> None:
    model, cache = policies(), ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))

        def committing(request: Any) -> Any:  # noqa: ANN401
            reader = opened.read(request)
            commit(opened, "DELETE FROM marts.readings WHERE reading_id = 2")
            return reader

        first = ask(opened, cache, BY_ZONE, context, model, read=committing)
        fresh = caller(opened, model, "u-exempt", ("management",))
        second = ask(opened, cache, BY_ZONE, fresh, model)
        assert origin(second) == "miss"  # a later snapshot: never the stored entry
        same(second, ask(opened, None, BY_ZONE, fresh, model))
        assert ce.rows_of(first) != ce.rows_of(second)


def test_two_workers_never_serve_an_earlier_snapshot() -> None:
    model = policies()
    first_worker, second_worker = ResultCache(), ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        ask(opened, first_worker, BY_ZONE, context, model)
        ask(opened, second_worker, BY_ZONE, context, model)
        commit(opened, "DELETE FROM marts.readings WHERE reading_id = 3")  # a run, through either
        fresh = caller(opened, model, "u-exempt", ("management",))
        for worker in (first_worker, second_worker):
            found = ask(opened, worker, BY_ZONE, fresh, model)
            assert origin(found) == "miss"
            same(found, ask(opened, None, BY_ZONE, fresh, model))


def test_the_lookup_reads_no_latest_snapshot_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    model, cache = policies(), ResultCache()
    with lake() as opened:
        context = caller(opened, model, "u-exempt", ("management",))
        pinned = engine(opened, cache)
        answer(json.dumps(BY_ZONE), FOUND.compiled, model, context, pinned)

        def refused(*_: object) -> None:
            raise AssertionError("the cache asked the lake for its latest snapshot")

        monkeypatch.setattr(Lake, "current_snapshot", refused)
        found = answer(json.dumps(BY_ZONE), FOUND.compiled, model, context, pinned)
        assert origin(found) == "hit"
