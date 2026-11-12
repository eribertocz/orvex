"""Masking on every engine (#442 AC4, precision 1): on the managed lake, the existing DuckLake
(SQLite and PostgreSQL catalogs, through the questions it serves, P5-14) and ClickHouse (CI), a
caller who sees a member masked is refused grouping, filtering, sorting and distinct counting on
it before any read, and a metric derived from it is hidden. A caller who may see the member
gets the same answer on every engine. A cell condition (`visible_when`) runs on the managed lake
only and is refused before any read elsewhere.

On a copy of the `direct-query` fixture whose `security.yaml` masks `outages.zone_id` (except
for `operations`) and puts a cell condition on `outages.minutes`.
"""

from __future__ import annotations

import dataclasses
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_security_scenarios import SEPARATOR
from tests.support.security_engines import ask, served

BASE = sf.fixture("direct-query", ce.VENDORS[0])
OUTAGES: list[list[str]] = [[str(v) for v in r] for r in BASE.rows["outages"]]
COLUMNS = [
    {"member": "outages.zone_id", "default": "masked", "mask": "***",
     "visible_to_groups": ["operations"]},
    {"member": "outages.minutes", "default": "visible", "mask": "***",
     "visible_when": "zone_id = $zone_id", "visible_to_groups": ["operations"]},
]  # fmt: skip
ENGINES = [("managed", ""), ("existing-ducklake", "sqlite"), ("existing-ducklake", "postgres"),
           ("clickhouse", "")]  # fmt: skip


@pytest.fixture(scope="module")
def found(tmp_path_factory: pytest.TempPathFactory) -> sf.Fixture:
    folder = Path(tmp_path_factory.mktemp("masked")) / "direct-query"
    shutil.copytree(BASE.folder, folder)
    policies = yaml.safe_load((folder / "security.yaml").read_text("utf-8"))
    policies["column_policies"] = COLUMNS
    (folder / "security.yaml").write_text(yaml.safe_dump(policies), "utf-8")
    return dataclasses.replace(BASE, folder=folder)


def caller(user: str, groups: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "kind": "security_context", "version": 1, "user": user, "project": "direct-query",
        "role": "author", "groups": sorted(groups), "attributes": {},
        "permission_fingerprint": "sha256:" + "0" * 64,
    }  # fmt: skip


def aggregate(**body: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "kind": "semantic-query",
        "body": {"kind": "semantic_query", "version": 1, "mode": "aggregate", **body},
    }


ZONE = {"member": "outages.zone_id"}
REFUSED = {
    "group": (aggregate(metrics=[{"metric": "outage_count"}], group_by=[ZONE]), "MEMBER_MASKED"),
    "filter": (aggregate(metrics=[{"metric": "outage_count"}], filters=[{
        "scope": "visual", **ZONE, "predicate": {
            "op": "in", "values": [{"type": "text", "value": "Z1"}], "include_null": False}}]),
        "MEMBER_MASKED"),
    "sort": (aggregate(metrics=[{"metric": "outage_count"}], order=[{"by": "outages.zone_id"}]),
             "MEMBER_MASKED"),
    "distinct count": (aggregate(metrics=[{"metric": "zone_count"}]), "MEMBER_NOT_FOUND"),
    "derived from a cell condition": (aggregate(metrics=[{"metric": "outage_minutes"}]),
                                      "MEMBER_NOT_FOUND"),
}  # fmt: skip


@pytest.mark.parametrize(("engine", "catalog"), ENGINES, ids=[f"{e}-{c}" for e, c in ENGINES])
def test_every_use_of_a_masked_member_is_refused_before_any_read_on_every_engine(
    found: sf.Fixture, engine: str, catalog: str
) -> None:
    with served(found, engine, catalog) as place:
        for use, (request, code) in REFUSED.items():
            received = ask(found, place, request, caller("u-zone1"))
            assert received.code == code, (use, received)
            tokens = {t for text in received.texts for t in SEPARATOR.split(text)}
            assert not {"Z1", "Z2", "Z3", "$zone_id"} & tokens, use
        assert not place.reads  # refused in planning: nothing was read


@pytest.mark.parametrize(("engine", "catalog"), ENGINES, ids=[f"{e}-{c}" for e, c in ENGINES])
def test_a_caller_who_may_see_the_member_gets_the_same_answer_on_every_engine(
    found: sf.Fixture, engine: str, catalog: str
) -> None:
    body = {"metrics": [{"metric": "outage_minutes"}, {"metric": "zone_count"}],
            "group_by": [ZONE], "order": [{"by": "outages.zone_id"}]}  # fmt: skip
    by_zone: dict[str, list[Decimal]] = {}
    for _, zone, minutes in OUTAGES:
        by_zone.setdefault(zone, []).append(Decimal(minutes))
    expected = [[z, f"{sum(m):.2f}", "1"] for z, m in sorted(by_zone.items())]
    with served(found, engine, catalog) as place:
        received = ask(found, place, aggregate(**body), caller("u-zone1", ("operations",)))
    assert received.code is None, received
    assert received.rows == expected


@pytest.mark.parametrize(
    ("engine", "catalog"), ENGINES[1:], ids=[f"{e}-{c}" for e, c in ENGINES[1:]]
)
def test_a_cell_condition_is_refused_before_any_read_off_the_managed_lake(
    found: sf.Fixture, engine: str, catalog: str
) -> None:
    request = {"kind": "semantic-query", "body": {
        "kind": "semantic_query", "version": 1, "mode": "detail",
        "fields": [{"member": "outages.outage_id"}, {"member": "outages.minutes"}],
        "order": [{"by": "outages.outage_id", "direction": "asc"}],
        "page": {"size": "10"}}}  # fmt: skip
    with served(found, engine, catalog) as place:
        received = ask(found, place, request, caller("u-zone1"))
        assert received.code is not None, received
        assert received.rows is None
        assert not place.reads
    if engine == "clickhouse":
        assert received.code == "POLICY_NOT_SUPPORTED_ON_ENGINE"
