"""The `partition_overwrite` check that a run loaded whole partitions (#155)."""

from __future__ import annotations

from orvex.transform.incremental_types import Check, Columns, quote_identifier
from orvex.transform.models import Model

_CUT_SHOWN = 5  # partitions named in the PARTITION_INCOMPLETE message


_LABEL_LENGTH = 60  # characters of one partition value shown in the message
_TEXT_TYPES = frozenset({"VARCHAR"})
_QUOTE = "''''"  # a SQL string literal holding one single quote


def _label(alias: str, kind: str) -> str:
    """A partition value as message text: `NULL` for SQL NULL, text values quoted (so the text
    `NULL` reads `'NULL'`), values longer than `_LABEL_LENGTH` characters shortened (before
    quoting, so a shortened text value keeps its closing quote)."""
    text = f"CAST({alias} AS VARCHAR)"
    shown = (
        f"CASE WHEN length({text}) > {_LABEL_LENGTH} "
        f"THEN left({text}, {_LABEL_LENGTH}) || '…' ELSE {text} END"
    )
    if kind in _TEXT_TYPES or kind.startswith("ENUM("):
        shown = f"{_QUOTE} || replace({shown}, {_QUOTE}, '''''') || {_QUOTE}"
    return f"coalesce({shown}, 'NULL')"


def cut_partitions_check(model: Model, target: str, stage: str, output: Columns) -> Check:
    """Fails when the `$since` filter loaded only part of a loaded partition (#155, decision
    #128 option B; #109 before it).

    A loaded partition is cut when the table holds dated rows of it older than the oldest dated
    loaded row of it (or the loaded rows of it have no dated row), or holds rows of it with a
    `NULL` time column while the loaded rows have none (a `$since` filter never reloads those).
    With the filter a lower bound of `time_column` (validation, `since_is_a_lower_bound`), a
    run loads every row of a partition from some time on, so a partition it loads in part has
    older rows left in the table (`> $since`, `>= $since`, a lookback). Partitions compare with
    `IS NOT DISTINCT FROM`, as the delete does, so `NULL` is a partition. Rows with the same
    time as the oldest loaded row but not reloaded are not detected: with the filter a lower
    bound, only another condition of the WHERE clause could leave them out.

    It also fails when the source itself no longer has a partition's oldest rows (then
    `--full-refresh` reloads it).

    One grouped pass over the loaded rows and one over the table's rows of loaded partitions.
    Partition columns are renamed `p0`, `p1`, … inside the query, so no column name can collide
    with the query's own names. The result is `'0'`, or up to five cut partitions, ordered by
    value, and their total.
    """
    incremental = model.config.incremental
    assert incremental is not None
    assert incremental.partition_by
    types = {name.lower(): kind for name, kind in output}
    columns = [quote_identifier(column.root) for column in incremental.partition_by]
    aliases = [f"p{index}" for index in range(len(columns))]
    pairs = list(zip(columns, aliases, strict=True))
    renamed = ", ".join(f"{column} AS {alias}" for column, alias in pairs)
    time = quote_identifier(incremental.time_column.root)

    def matches(left: str, left_names: list[str]) -> str:
        return " AND ".join(
            f"{left}.{name} IS NOT DISTINCT FROM loaded.{alias}"
            for name, alias in zip(left_names, aliases, strict=True)
        )

    # `min` skips NULL times: `oldest` is NULL when a partition has no dated row.
    counted = f"min({time}) AS oldest, count(*) FILTER (WHERE {time} IS NULL) AS n_undated"
    labels = [
        _label(f"loaded.{alias}", types.get(column.root.lower(), ""))
        for column, alias in zip(incremental.partition_by, aliases, strict=True)
    ]
    label = labels[0] if len(labels) == 1 else "'(' || " + " || ', ' || ".join(labels) + " || ')'"
    order = ", ".join(f"loaded.{alias} NULLS LAST" for alias in aliases)
    sql = (
        f"WITH loaded AS (SELECT {renamed}, {counted} FROM {stage} GROUP BY ALL), "
        f"held AS (SELECT {renamed}, {counted} FROM {target} AS row_held "
        f"WHERE EXISTS (SELECT 1 FROM loaded WHERE {matches('row_held', columns)}) "
        "GROUP BY ALL), "
        f"cut AS (SELECT {label} AS cut_label, row_number() OVER (ORDER BY {order}) AS cut_rank "
        f"FROM loaded JOIN held ON {matches('held', aliases)} "
        "WHERE (held.oldest IS NOT NULL "
        "AND (loaded.oldest IS NULL OR held.oldest < loaded.oldest)) "
        "OR (held.n_undated > 0 AND loaded.n_undated = 0)) "
        "SELECT CASE WHEN count(*) = 0 THEN '0' ELSE "
        f"string_agg(cut_label, ', ' ORDER BY cut_rank) FILTER (WHERE cut_rank <= {_CUT_SHOWN}) "
        "|| ' (' || count(*) || ' in total)' END FROM cut"
    )
    name = incremental.time_column.root
    partition_by = ", ".join(column.root for column in incremental.partition_by)
    return Check(
        sql,
        "PARTITION_INCOMPLETE",
        f"{model.name}: the $since filter loaded only part of these partitions ({partition_by}): "
        "{detail}. The table holds rows of them older than the oldest row this run loaded of "
        f"them, or with a NULL {name}, and partition_overwrite would delete them; nothing is "
        "written. Load whole partitions, for example "
        f"WHERE {name} >= date_trunc('month', $since) for monthly partitions; if the source "
        "itself no longer has those rows, use --full-refresh.",
        (),
        detail=True,
    )
