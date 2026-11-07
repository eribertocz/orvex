"""The independent oracles of the property tests (P2-28, #367; Owner correction 1).

`expected` is a reference evaluator in pure Python with `Decimal`: it takes the generated
world, query and caller as plain data and computes the answer by direct evaluation — joins by
dictionary lookups, aggregation by `Decimal` sums, the ratio recomputed per group, policies as
row filters. It imports nothing from `orvex`: it shares no binder, planner, compiled model or
SQL with the engine.

`duckdb_expected` is the second oracle: plain SQL written here, independent of the engine's
adapter, over the same rows in a throwaway in-memory DuckDB. It covers the additive and
distinct metrics, grouping, filters, the join and the row policy; the ratio's rounding is the
pure-Python oracle's to check, so a query with `unit_price` is not sent to it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

Row = dict[str, object]
ZONE = ZoneInfo("America/Santiago")
CENT = Decimal("0.01")


def _parse(table: str, rows: list[dict[str, str | None]]) -> list[Row]:
    kinds: dict[str, Callable[[str], object]] = {
        "customer_id": int,
        "sale_id": int,
        "amount": Decimal,
        "qty": Decimal,
        "sold_on": date.fromisoformat,
        "sold_at": lambda text: datetime.fromisoformat(text.replace(" ", "T")),
    }
    return [
        {
            name: (None if value is None else kinds.get(name, str)(value))
            for name, value in row.items()
        }
        for row in rows
    ]


def _joined(customers: list[Row], sales: list[Row]) -> list[Row]:
    """Each sale with its customer's attributes; no match leaves them NULL (the engine keeps
    the row in the NULL group)."""
    by_id = {customer["customer_id"]: customer for customer in customers}
    joined = []
    for sale in sales:
        customer = by_id.get(sale["customer_id"])
        joined.append(
            {
                **{f"sales.{name}": value for name, value in sale.items()},
                "customers.region": customer["region"] if customer else None,
                "customers.segment": customer["segment"] if customer else None,
                "customers.customer_id": customer["customer_id"] if customer else None,
            }
        )
    return joined


def _value(entry: Mapping[str, Any]) -> object:
    kinds: dict[str, Callable[[str], object]] = {
        "decimal": Decimal,
        "date": date.fromisoformat,
        "integer": int,
        "text": str,
    }
    return kinds[entry["type"]](entry["value"])


def _passes(row: Row, filters: Sequence[Mapping[str, Any]]) -> bool:
    """SQL semantics: a comparison with NULL keeps nothing unless `include_null` says so."""
    for entry in filters:
        value = row[entry["member"]]
        predicate = entry["predicate"]
        op = predicate["op"]
        if op == "is_null":
            if value is not None:
                return False
        elif op == "is_not_null":
            if value is None:
                return False
        elif op == "in":
            wanted = {_value(v) for v in predicate["values"]}
            if value is None:
                if not predicate.get("include_null"):
                    return False
            elif value not in wanted:
                return False
        elif op == "range":
            if value is None:
                return False
            for bound, keeps in (
                ("gte", lambda v, b: v >= b),
                ("gt", lambda v, b: v > b),
                ("lte", lambda v, b: v <= b),
                ("lt", lambda v, b: v < b),
            ):
                if bound in predicate and not keeps(value, _value(predicate[bound])):  # type: ignore[no-untyped-call]
                    return False
        else:  # a predicate the oracle does not know is a defect of the generator
            raise AssertionError(f"the oracle does not evaluate {op!r}")
    return True


def _dimension(row: Row, entry: Mapping[str, Any]) -> object:
    value = row[entry["member"]]
    if entry.get("bucket", {}).get("unit") == "month" and value is not None:
        if isinstance(value, datetime):  # an instant buckets in the model's zone
            local = value.astimezone(ZONE)
            return date(local.year, local.month, 1)
        if isinstance(value, date):
            return date(value.year, value.month, 1)
    return value


def _decimals(rows: list[Row], column: str) -> list[Decimal]:
    return [value for row in rows if isinstance(value := row[column], Decimal)]


def _metric(name: str, rows: list[Row]) -> object:
    if name == "revenue":
        values = _decimals(rows, "sales.amount")
        return sum(values, Decimal(0)) if values else None
    if name == "buyers":
        buyers = {row["sales.customer_id"] for row in rows}
        return len(buyers - {None})
    if name == "unit_price":  # recomputed per group; a zero or empty denominator is NULL
        amounts = _decimals(rows, "sales.amount")
        quantities = _decimals(rows, "sales.qty")
        if not amounts or not quantities:
            return None
        denominator = sum(quantities, Decimal(0))
        if not denominator:
            return None
        return (sum(amounts, Decimal(0)) / denominator).quantize(CENT, ROUND_HALF_UP)
    raise AssertionError(f"the oracle does not compute {name!r}")


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        quantized = value.quantize(CENT)
        if quantized == 0:
            quantized = abs(quantized)  # a decimal zero has no sign
        return f"{quantized:f}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def visible(
    customers: list[dict[str, str | None]],
    sales: list[dict[str, str | None]],
    channel: str | None,
) -> list[Row]:
    """The joined rows the caller may see: the row policy keeps the sales of the caller's
    channel, and a NULL channel matches nothing (SQL semantics)."""
    joined = _joined(_parse("customers", customers), _parse("sales", sales))
    if channel is None:
        return joined
    return [row for row in joined if row["sales.channel"] == channel]


def expected(
    customers: list[dict[str, str | None]],
    sales: list[dict[str, str | None]],
    query: Mapping[str, Any],
    channel: str | None = None,
) -> list[list[str | None]]:
    """The exact rows of `query`, as `store/v1` JSON text, unordered (sort both sides)."""
    rows = [
        row for row in visible(customers, sales, channel) if _passes(row, query.get("filters", ()))
    ]
    if query["mode"] == "detail":
        members = [entry["member"] for entry in query["fields"]]
        return [[_text(row[member]) for member in members] for row in rows]
    entries = query.get("group_by", [])
    groups: dict[tuple[object, ...], list[Row]] = {}
    for row in rows:
        key = tuple(_dimension(row, entry) for entry in entries)
        groups.setdefault(key, []).append(row)
    if not entries and not groups:
        groups[()] = []  # a grand total exists even over nothing
    metrics = [entry["metric"] for entry in query["metrics"]]
    return [
        [*(_text(value) for value in key), *(_text(_metric(name, found)) for name in metrics)]
        for key, found in groups.items()
    ]


# --- the second oracle ---------------------------------------------------------------------------


def _sql_filters(filters: Sequence[Mapping[str, Any]]) -> tuple[str, list[object]]:
    pieces, bound = [], []
    columns = {  # this oracle's own spelling, not the engine's
        "customers.region": "c.region",
        "customers.segment": "c.segment",
        "sales.amount": "s.amount",
        "sales.sold_on": "s.sold_on",
        "sales.customer_id": "s.customer_id",
        "sales.channel": "s.channel",
    }
    for entry in filters:
        column = columns[entry["member"]]
        predicate = entry["predicate"]
        op = predicate["op"]
        if op == "is_null":
            pieces.append(f"{column} IS NULL")
        elif op == "is_not_null":
            pieces.append(f"{column} IS NOT NULL")
        elif op == "in":
            marks = ", ".join("?" for _ in predicate["values"])
            piece = f"{column} IN ({marks})"
            if predicate.get("include_null"):
                piece = f"({piece} OR {column} IS NULL)"
            pieces.append(piece)
            bound += [v["value"] for v in predicate["values"]]
        elif op == "range":
            for bound_name, sign in (("gte", ">="), ("gt", ">"), ("lte", "<="), ("lt", "<")):
                if bound_name in predicate:
                    kind = {"decimal": "DECIMAL(18,2)", "date": "DATE"}[
                        predicate[bound_name]["type"]
                    ]
                    pieces.append(f"{column} {sign} CAST(? AS {kind})")
                    bound.append(predicate[bound_name]["value"])
    return (" AND ".join(pieces) if pieces else "TRUE"), bound


def duckdb_expected(
    customers: list[dict[str, str | None]],
    sales: list[dict[str, str | None]],
    query: Mapping[str, Any],
    channel: str | None = None,
) -> list[list[str | None]]:
    """Plain SQL on the same rows, written here: sums, distinct counts, grouping, filters, the
    join and the row policy. Not asked about the ratio (its rounding is the Python oracle's)."""
    import duckdb

    connection = duckdb.connect()
    try:
        connection.execute(f"SET TimeZone = '{ZONE.key}'")
        connection.execute(
            "CREATE TABLE customers (customer_id BIGINT, region VARCHAR, segment VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE sales (sale_id BIGINT, customer_id BIGINT, amount DECIMAL(18,2), "
            "qty DECIMAL(18,2), sold_on DATE, sold_at TIMESTAMPTZ, channel VARCHAR)"
        )
        for table, rows, names in (
            ("customers", customers, ("customer_id", "region", "segment")),
            (
                "sales",
                sales,
                ("sale_id", "customer_id", "amount", "qty", "sold_on", "sold_at", "channel"),
            ),
        ):
            for row in rows:
                marks = ", ".join("?" for _ in names)
                connection.execute(f"INSERT INTO {table} VALUES ({marks})", [row[n] for n in names])
        policy = "" if channel is None else " AND s.channel = ?"
        where, bound = _sql_filters(query.get("filters", ()))
        if query["mode"] == "detail":
            columns = {
                "sales.sale_id": "s.sale_id",
                "sales.amount": "s.amount",
                "sales.channel": "s.channel",
                "customers.region": "c.region",
            }
            selected = ", ".join(columns[e["member"]] for e in query["fields"])
            sql = (
                f"SELECT {selected} FROM sales AS s LEFT JOIN customers AS c "
                f"USING (customer_id) WHERE ({where}){policy}"
            )
        else:
            dimensions = []
            for entry in query.get("group_by", []):
                if entry.get("bucket", {}).get("unit") == "month":
                    column = {"sales.sold_on": "s.sold_on", "sales.sold_at": "s.sold_at"}[
                        entry["member"]
                    ]
                    dimensions.append(f"CAST(date_trunc('month', {column}) AS DATE)")
                else:
                    dimensions.append(
                        {
                            "sales.channel": "s.channel",
                            "customers.region": "c.region",
                            "customers.segment": "c.segment",
                        }[entry["member"]]
                    )
            aggregates = {
                "revenue": "SUM(s.amount)",
                "buyers": "COUNT(DISTINCT s.customer_id)",
            }
            selected = ", ".join(
                [*dimensions, *(aggregates[e["metric"]] for e in query["metrics"])]
            )
            grouped = f" GROUP BY {', '.join(dimensions)}" if dimensions else ""
            sql = (
                f"SELECT {selected} FROM sales AS s LEFT JOIN customers AS c "
                f"USING (customer_id) WHERE ({where}){policy}{grouped}"
            )
        found = connection.execute(sql, [*bound, *([] if channel is None else [channel])])
        rows_out: list[list[str | None]] = []
        for record in found.fetchall():
            rows_out.append([None if v is None else _text(_as_oracle(v)) for v in record])
        return rows_out
    finally:
        connection.close()


def _as_oracle(value: object) -> object:
    """DuckDB's Python values in the oracle's own forms, so `_text` writes them alike."""
    if isinstance(value, float):
        raise AssertionError("an exact column came back as a float")
    if isinstance(value, datetime):
        return value.date() if value.time() == datetime.min.time() else value
    return value
