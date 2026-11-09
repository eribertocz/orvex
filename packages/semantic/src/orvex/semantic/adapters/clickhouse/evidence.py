"""Evidence checks written for ClickHouse (P2-31 PR 4, #225; ADR-0027 §7): the statements that
count what a live engine's data violates of the model's declarations, as the DuckDB adapter's
checks count it on the managed lake (`adapters.duckdb`, `unique_check` and the rest).

Each statement has the reference's shape, with this adapter's names, values and functions:
each table read whole with no row policy, an overlap's rows written twice (numbered by their
values) rather than as a `WITH`, every count a `BIGINT` (`toInt64(count(*))`: a
ClickHouse `count` is unsigned), and every statement checked against the allow-lists before it
is returned. An aggregate table is never read on a live engine, so `consistent` is not checked
here (`PlanError`: it stays `declared`); `append_only` compares snapshots, which a live engine
does not have. Nor is a check whose key is a float (ClickHouse compares floats differently).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from orvex.semantic.adapters.clickhouse.allowlist import check_statement
from orvex.semantic.adapters.clickhouse.writer import _ClickHouse
from orvex.semantic.adapters.dialect import quote
from orvex.semantic.adapters.duckdb import EvidenceCheck
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import PlanError

Pair = tuple[MemberId, MemberId]
COUNT = "toInt64(count(*))"


class _Checks(_ClickHouse):
    def __init__(
        self,
        compiled: CompiledModel,
        columns: Mapping[str, Iterable[str]],
        written: dict[str, tuple[str, ...]],
        **aliases: str,
    ) -> None:
        """No plan: the model's tables, each read whole under its alias (`a="t"` for `#a`)."""
        self.written = written
        self.model(compiled, None, None)
        self.values = None
        self.dataset_alias.update({f"#{k}": v for k, v in aliases.items()})
        self.catalog(compiled, columns)

    def done(self, statement: str, counts: tuple[str, ...]) -> EvidenceCheck:
        tables = tuple(dict.fromkeys(table for table, _ in self.reads))
        check_statement(statement, set(tables))
        self.written[statement] = counts
        return EvidenceCheck(statement, tuple(self.parameters), counts, tables)

    def keys(self, fields: tuple[MemberId, ...], alias: str) -> list[str]:
        return [self.field(f, alias) for f in fields]

    def matched(self, pairs: tuple[Pair, ...], here: str, there: str, columns: str) -> str:
        return " AND ".join(
            f"{self.field(a, here)} = {quote(there)}.{quote(f'{columns}{i}')}"
            for i, (a, _) in enumerate(pairs)
        )


class ClickHouseChecks:
    """The evidence statements of one ClickHouse connection, whose tables have `columns` (each
    table's column names as the engine's catalog holds them). `orvex.semantic.evidence.verify`
    calls it as it calls the DuckDB adapter's checks."""

    def __init__(self, columns: Mapping[str, Iterable[str]]) -> None:
        self.columns = {table: tuple(names) for table, names in columns.items()}
        # Each statement written, with its count columns: what a read of it returns.
        self.written: dict[str, tuple[str, ...]] = {}

    def _writer(self, compiled: CompiledModel, *keys: MemberId, **aliases: str) -> _Checks:
        """A writer for one check whose `keys` are compared; a float key is never compared
        here (ClickHouse keeps -0.0 and 0.0 apart and `NaN` from `NaN`): `PlanError`, so the
        evidence stays `declared`."""
        if any(compiled.require(k).typed.type.logical.datatype == "Float" for k in keys):
            raise PlanError("a float key is compared differently on ClickHouse")
        return _Checks(compiled, self.columns, self.written, **aliases)

    def unique(
        self, compiled: CompiledModel, dataset: str, key: tuple[MemberId, ...]
    ) -> EvidenceCheck:
        w = self._writer(compiled, *key, a=dataset)
        keys = ", ".join(w.keys(key, "#a"))
        return w.done(
            f"SELECT {COUNT} AS {quote('#duplicates')} FROM (SELECT 1 FROM "
            f"{w.relation(dataset)} AS {quote('#a')} GROUP BY {keys} HAVING count(*) > 1) AS "
            f"{quote('#v')}",
            ("#duplicates",),
        )

    def overlap(
        self,
        compiled: CompiledModel,
        dataset: str,
        entity: tuple[MemberId, ...],
        start: MemberId,
        end: MemberId,
        referenced: tuple[str, tuple[Pair, ...]] | None = None,
    ) -> EvidenceCheck:
        joined = [m for pair in (referenced[1] if referenced else ()) for m in pair]
        w = self._writer(
            compiled, *entity, *joined, a=dataset, b=referenced[0] if referenced else dataset
        )

        def rows() -> str:
            """The entity's rows with both bounds, numbered in the order of every value they
            are compared by: rows that tie are alike, so either numbering counts each pair once.
            Written twice, not as a `WITH`: the allow-list reads only a plan's tables."""
            e = w.keys(entity, "#a")
            a, b = w.field(start, "#a"), w.field(end, "#a")
            read = ", ".join(
                [
                    f"row_number() OVER (ORDER BY {', '.join([*e, a, b])}) AS {quote('#n')}",
                    *(f"{v} AS {quote(f'#e{i}')}" for i, v in enumerate(e)),
                    f"{a} AS {quote('#f')}",
                    f"{b} AS {quote('#u')}",
                ]
            )
            where = f"{a} IS NOT NULL AND {b} IS NOT NULL"
            if referenced is not None:
                source, pairs = referenced
                on = " AND ".join(f"{w.field(f, '#b')} = {w.field(t, '#a')}" for f, t in pairs)
                where += (
                    f" AND EXISTS (SELECT 1 FROM {w.relation(source)} AS {quote('#b')} WHERE {on})"
                )
            return f"(SELECT {read} FROM {w.relation(dataset)} AS {quote('#a')} WHERE {where})"

        x, y = quote("#x"), quote("#y")
        same = " AND ".join(
            f"{x}.{quote(f'#e{i}')} IS NOT DISTINCT FROM {y}.{quote(f'#e{i}')}"
            for i in range(len(entity))
        )
        return w.done(
            f"SELECT {COUNT} AS {quote('#overlaps')} FROM {rows()} AS {x}"
            f" JOIN {rows()} AS {y} ON {same} AND {x}.{quote('#n')} < {y}.{quote('#n')}"
            f" AND {x}.{quote('#f')} < {y}.{quote('#u')} AND {y}.{quote('#f')} < {x}.{quote('#u')}",
            ("#overlaps",),
        )

    def repeat(
        self, compiled: CompiledModel, side: str, other: str, pairs: tuple[Pair, ...]
    ) -> EvidenceCheck:
        w = self._writer(compiled, *(m for pair in pairs for m in pair), a=side, b=other)
        read = ", ".join(
            f"{w.field(s, '#a')} AS {quote(f'#k{i}')}" for i, (_, s) in enumerate(pairs)
        )
        keys = ", ".join(w.field(s, "#a") for _, s in pairs)
        on = w.matched(pairs, "#b", "#v", "#k")
        return w.done(
            f"SELECT {COUNT} AS {quote('#repeated')} FROM (SELECT {read} FROM "
            f"{w.relation(side)} AS {quote('#a')} GROUP BY {keys} HAVING count(*) > 1) AS "
            f"{quote('#v')} WHERE EXISTS (SELECT 1 FROM {w.relation(other)} AS {quote('#b')}"
            f" WHERE {on})",
            ("#repeated",),
        )

    def orphan(
        self, compiled: CompiledModel, source: str, target: str, pairs: tuple[Pair, ...]
    ) -> EvidenceCheck:
        w = self._writer(compiled, *(m for pair in pairs for m in pair), a=source, b=target)
        read = ", ".join(
            f"{w.field(f, '#a')} AS {quote(f'#k{i}')}" for i, (f, _) in enumerate(pairs)
        )
        known = " AND ".join(f"{w.field(f, '#a')} IS NOT NULL" for f, _ in pairs)
        on = w.matched(tuple((t, f) for f, t in pairs), "#b", "#v", "#k")
        return w.done(
            f"SELECT {COUNT} AS {quote('#orphans')} FROM (SELECT DISTINCT {read} FROM "
            f"{w.relation(source)} AS {quote('#a')} WHERE {known}) AS {quote('#v')}"
            f" WHERE NOT EXISTS (SELECT 1 FROM {w.relation(target)} AS {quote('#b')} WHERE {on})",
            ("#orphans",),
        )

    def consistency(self, compiled: CompiledModel, *_: Any) -> EvidenceCheck:  # noqa: ANN401
        raise PlanError("an aggregate table is read on the managed lake only")
