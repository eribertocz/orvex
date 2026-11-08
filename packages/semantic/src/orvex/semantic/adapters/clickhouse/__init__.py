"""The ClickHouse adapter (P2-31, #225; ADR-0027 §8, ADR-0028 §3): a `SemanticPlan` as one
ClickHouse `SELECT` with bound parameters, for the store's ClickHouse read boundary
(`contracts/store/v1`, "ClickHouse Direct Query").

One purpose per module:

- `writer`: the statement of a plan, each dialect difference encoded explicitly;
- `exact`: store types in ClickHouse and the checked conversion to a declared type;
- `allowlist`: the statements the adapter may send;
- `names`: quoting the engine's names;
- `cost`: `EXPLAIN ESTIMATE` and the budget check (law 7).
"""

from orvex.semantic.adapters.clickhouse.allowlist import FUNCTIONS, check_statement
from orvex.semantic.adapters.clickhouse.cost import (
    estimate_statement,
    refuse_over_budget,
    rows_scanned,
)
from orvex.semantic.adapters.clickhouse.exact import OUT_OF_RANGE, canonical, engine_type
from orvex.semantic.adapters.clickhouse.names import quote_name
from orvex.semantic.adapters.clickhouse.writer import CAPABILITIES, NAME, VERSION, compile_plan

__all__ = [
    "CAPABILITIES",
    "FUNCTIONS",
    "NAME",
    "OUT_OF_RANGE",
    "VERSION",
    "canonical",
    "check_statement",
    "compile_plan",
    "engine_type",
    "estimate_statement",
    "quote_name",
    "refuse_over_budget",
    "rows_scanned",
]
