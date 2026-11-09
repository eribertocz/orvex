"""The ClickHouse adapter (P2-31, #225; ADR-0027 §8, ADR-0028 §3): a `SemanticPlan` as one
ClickHouse `SELECT` with bound parameters, for the store's ClickHouse read boundary
(`contracts/store/v1`, "ClickHouse Direct Query").

One purpose per module:

- `writer`: the statement of a plan, each dialect difference encoded explicitly;
- `exact`: store types in ClickHouse and the checked conversion to a declared type;
- `allowlist`: the statements the adapter may send;
- `names`: quoting the engine's names;
- `cost`: `EXPLAIN ESTIMATE`, the estimate a receipt records (law 7);
- `evidence`: the statements checking the model's evidence on the engine (ADR-0027 §7);
- `zones`: instants in a zone, and the check of the engine's zone data (C6).
"""

from orvex.semantic.adapters.clickhouse.allowlist import FUNCTIONS, check_statement
from orvex.semantic.adapters.clickhouse.cost import (
    estimate_statement,
    rows_scanned,
)
from orvex.semantic.adapters.clickhouse.evidence import ClickHouseChecks
from orvex.semantic.adapters.clickhouse.exact import OUT_OF_RANGE, canonical, engine_type
from orvex.semantic.adapters.clickhouse.names import quote_name
from orvex.semantic.adapters.clickhouse.writer import CAPABILITIES, NAME, VERSION, compile_plan
from orvex.semantic.adapters.clickhouse.zones import CHECK_SQL as ZONE_CHECK_SQL

__all__ = [
    "CAPABILITIES",
    "FUNCTIONS",
    "NAME",
    "OUT_OF_RANGE",
    "VERSION",
    "ZONE_CHECK_SQL",
    "ClickHouseChecks",
    "canonical",
    "check_statement",
    "compile_plan",
    "engine_type",
    "estimate_statement",
    "quote_name",
    "rows_scanned",
]
