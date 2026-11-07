"""The generated world of the property tests (P2-28, #367).

One small model family — a `sales` fact joined many-to-one to a `customers` dimension — with
three metrics (an additive sum, a holistic distinct count, a recomputable ratio), bounded
generated rows from adversarial pools, and generated aggregate and detail questions that are
valid by construction. The engine answers them through the same public entry as everything
else (`orvex.semantic.execution.answer`); the oracles are in `tests.properties.oracle`.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.execution import Answer, answer
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.result.cursors import SealedForm
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
ZONE = "America/Santiago"

# The run's profile (Owner correction 5): deterministic and small in CI and locally, so a pull
# request's run is stable and inside the budget; wide and random in the nightly workflow, which
# sets PROPERTY_PROFILE=nightly. Applied per test, so nothing outside this package changes.
NIGHTLY = os.environ.get("PROPERTY_PROFILE") == "nightly"
PROFILE = settings(
    max_examples=400 if NIGHTLY else 10,
    derandomize=not NIGHTLY,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
    ],
)  # a zone with DST, so generated instants cross its transitions

CATALOG: dict[str, dict[str, SourceColumn]] = {
    "customers": {
        "customer_id": SourceColumn("BIGINT", False),
        "region": SourceColumn("VARCHAR"),
        "segment": SourceColumn("VARCHAR"),
    },
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT"),
        "amount": SourceColumn("DECIMAL(18,2)"),
        "qty": SourceColumn("DECIMAL(18,2)", False),
        "sold_on": SourceColumn("DATE", False),
        "sold_at": SourceColumn("TIMESTAMPTZ", False),
        "channel": SourceColumn("VARCHAR"),
    },
}


def _model() -> CompiledModel:
    ratio = {
        "element": "metric",
        "additivity": "recomputable",
        "components": {"numerator": "SUM(sales.amount)", "denominator": "SUM(sales.qty)"},
        "zero_denominator": "null",
        "rounding": {"scale": 2, "mode": "half_away_from_zero"},
    }
    distinct = {"element": "metric", "aggregate_class": "holistic", "additivity": "non_additive"}
    document = {
        "version": "0.2.0.dev0",
        "name": "world",
        "datasets": [
            {
                "name": "customers",
                "source": "customers",
                "primary_key": ["customer_id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}}
                    for n in ("customer_id", "region", "segment")
                ],
            },
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}}
                    for n in (
                        "sale_id",
                        "customer_id",
                        "amount",
                        "qty",
                        "sold_on",
                        "sold_at",
                        "channel",
                    )
                ],
            },
        ],
        "relationships": [
            {
                "name": "buyer",
                "from": "sales",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
                "custom_extensions": [
                    extension({"element": "relationship", "cardinality": "many_to_one"})
                ],
            }
        ],
        "metrics": [
            {"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}},
            {
                "name": "buyers",
                "expression": {"sql": "COUNT(DISTINCT sales.customer_id)"},
                "custom_extensions": [extension(distinct)],
            },
            {
                "name": "unit_price",
                "expression": {"sql": "SUM(sales.amount) / SUM(sales.qty)"},
                "custom_extensions": [extension(ratio)],
            },
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": ZONE})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/world.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


COMPILED = _model()
METRICS = ("revenue", "buyers", "unit_price")
DIMENSIONS = ("sales.channel", "customers.region", "customers.segment")
HIDDEN_POLICY = {
    "version": 1,
    "column_policies": [{"member": "customers.segment", "default": "hidden"}],
}
ROW_POLICY = {
    "version": 1,
    "row_policies": [
        {
            "name": "own_channel",
            "dataset": "sales",
            "kind": "predicate",
            "predicate": "channel = $channel",
        }
    ],
}


def policies(document: Mapping[str, Any] | None = None) -> PolicyModel:
    import yaml

    return read_policies(
        yaml.safe_dump(dict(document or {"version": 1})),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )


NO_POLICIES = policies()


@dataclass(frozen=True)
class World:
    """The generated rows, as `store/v1` text values (None is SQL NULL)."""

    customers: list[dict[str, str | None]]
    sales: list[dict[str, str | None]]


@contextmanager
def lake_of(world: World) -> Iterator[Lake]:
    with tempfile.TemporaryDirectory() as folder:
        inputs = []
        for name, rows in (("customers", world.customers), ("sales", world.sales)):
            path = Path(folder) / f"{name}.csv"
            names = list(CATALOG[name])
            lines = [",".join(names)]
            lines += [",".join("" if r[n] is None else str(r[n]) for n in names) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            inputs.append(path)
        with ce.case_lake(inputs, CATALOG) as lake:
            yield lake


def ask(
    document: Document,
    lake: Lake,
    policy: PolicyModel = NO_POLICIES,
    context: Mapping[str, Any] | None = None,
    **engine: Any,  # noqa: ANN401
) -> Answer:
    snapshot = lake.current_snapshot()
    pinned = {"id": snapshot.id, "committed_at": snapshot.committed_at.isoformat()}
    extras: dict[str, Any] = {
        "cursors": SealedForm(b"p" * 32),
        "pinned": lambda version: pinned if version == str(snapshot.id) else None,
        **engine,
    }
    return answer(
        json.dumps(document),
        COMPILED,
        policy,
        dict(context or ce.viewer_context()),
        ce.engine_for(lake, **extras),
    )


def sorted_rows(rows: list[list[str | None]]) -> list[list[str | None]]:
    """One canonical order for a multiset comparison: the engine's own order is not asked."""
    return sorted(rows, key=lambda row: [(value is None, value or "") for value in row])


# --- strategies ----------------------------------------------------------------------------------

REGIONS = ("North", "South", "East", None)
SEGMENTS = ("a", "b", None)
CHANNELS = ("web", "shop", None)
# Exact edges: NULL, zero, negatives, a tie at the rounding scale, and large values.
AMOUNTS = (None, "0.00", "-1.00", "0.01", "0.05", "2.50", "10.00", "99999999.99", "-0.01")
QUANTITIES = ("0.00", "1.00", "2.00", "0.50", "3.00")
DAYS = ("2026-01-01", "2026-01-31", "2026-02-01", "2026-04-05", "2026-09-06", "2026-12-31")
# UTC instants around America/Santiago's DST transitions (the first Sundays of April and
# September 2026), both sides of each, plus month edges in that zone.
INSTANTS = (
    "2026-04-05 02:59:00+00",
    "2026-04-05 03:00:00+00",
    "2026-04-05 04:00:00+00",
    "2026-09-06 03:59:00+00",
    "2026-09-06 04:00:00+00",
    "2026-02-01 02:59:00+00",
    "2026-02-01 03:01:00+00",
    "2026-12-31 23:59:59+00",
)


@st.composite
def worlds(draw: st.DrawFn) -> World:
    count = draw(st.integers(0, 5))
    customers = [
        {
            "customer_id": str(number),
            "region": draw(st.sampled_from(REGIONS)),
            "segment": draw(st.sampled_from(SEGMENTS)),
        }
        for number in range(1, count + 1)
    ]
    sales_count = draw(st.integers(0, 12))
    sales = [
        {
            "sale_id": str(number),
            # 0 never exists and 9 may not: orphans land in the NULL group (left-join rule).
            "customer_id": draw(st.sampled_from((None, "1", "2", "3", "9"))),
            "amount": draw(st.sampled_from(AMOUNTS)),
            "qty": draw(st.sampled_from(QUANTITIES)),
            "sold_on": draw(st.sampled_from(DAYS)),
            "sold_at": draw(st.sampled_from(INSTANTS)),
            "channel": draw(st.sampled_from(CHANNELS)),
        }
        for number in range(1, sales_count + 1)
    ]
    return World(customers, sales)


def _filters(draw: st.DrawFn) -> list[Document]:
    pool: list[Document] = [
        {
            "scope": "visual",
            "member": "customers.region",
            "predicate": {
                "op": "in",
                "values": [{"type": "text", "value": "North"}, {"type": "text", "value": "East"}],
                "include_null": draw(st.booleans()),
            },
        },
        {
            "scope": "page",
            "member": "sales.amount",
            "predicate": {
                "op": "range",
                "gte": {"type": "decimal", "value": "0.01"},
                "lt": {"type": "decimal", "value": "100.00"},
            },
        },
        {
            "scope": "visual",
            "member": "sales.sold_on",
            "predicate": {
                "op": "range",
                "gte": {"type": "date", "value": "2026-01-31"},
                "lte": {"type": "date", "value": "2026-09-06"},
            },
        },
        {"scope": "visual", "member": "sales.customer_id", "predicate": {"op": "is_not_null"}},
        {"scope": "visual", "member": "sales.channel", "predicate": {"op": "is_null"}},
    ]
    chosen = draw(st.lists(st.sampled_from(range(len(pool))), max_size=2, unique=True))
    return [pool[index] for index in chosen]


@st.composite
def aggregate_queries(draw: st.DrawFn) -> Document:
    metrics = draw(
        st.lists(st.sampled_from(METRICS), min_size=1, max_size=len(METRICS), unique=True)
    )
    group_by = draw(st.lists(st.sampled_from(DIMENSIONS), max_size=2, unique=True))
    document: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": name} for name in metrics],
    }
    entries: list[Document] = [{"member": member} for member in group_by]
    bucketed = draw(st.sampled_from((None, "sales.sold_on", "sales.sold_at")))
    if bucketed is not None:
        entries.append({"member": bucketed, "bucket": {"unit": "month"}})
    if entries:
        document["group_by"] = entries
    filters = _filters(draw)
    if filters:
        document["filters"] = filters
    return document


@st.composite
def detail_queries(draw: st.DrawFn) -> Document:
    others = st.lists(
        st.sampled_from(("sales.amount", "sales.channel", "customers.region")),
        max_size=2,
        unique=True,
    )
    fields = ["sales.sale_id", *draw(others)]
    document: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": name} for name in fields],
        "order": [{"by": "sales.sale_id", "direction": "asc"}],
        "page": {"size": str(draw(st.integers(1, 5)))},
    }
    filters = _filters(draw)
    if filters:
        document["filters"] = filters
    return document


@dataclass(frozen=True)
class Caller:
    """A generated caller under the row policy, with its one channel attribute. A caller
    without the attribute sees zero rows (fail closed), which a directed test asserts."""

    channel: str
    context: dict[str, Any] = field(default_factory=dict)


def callers() -> st.SearchStrategy[Caller]:
    def build(channel: str) -> Caller:
        attributes = {"channel": {"type": "text", "value": channel}}
        return Caller(channel, ce.viewer_context(attributes=attributes))

    return st.sampled_from(("web", "shop")).map(build)


def decimal_text(value: Decimal | None, scale: int = 2) -> str | None:
    if value is None:
        return None
    quantized = value.quantize(Decimal(1).scaleb(-scale))
    if quantized == 0:
        quantized = abs(quantized)  # a decimal zero has no sign
    return f"{quantized:f}"
