"""Exact arithmetic end to end on the managed lake (#426): an exact value never passes through
a float. A question that would need one is refused before any read, naming only what the caller
may see; an exact quotient is only C2's declared division, rounded once as declared."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from orvex.semantic import SemanticError
from orvex.semantic.adapters import duckdb as duckdb_adapter
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness import analyze
from orvex.semantic.execution import answer
from orvex.semantic.explanation import explain
from orvex.semantic.ossie.reader import read
from orvex.semantic.planning import plan
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.query.binding import bind_query
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
CATALOG: Mapping[str, Mapping[str, SourceColumn]] = {
    "t": {
        "id": SourceColumn("INTEGER", nullable=False),
        "a": SourceColumn("INTEGER"),
        "b": SourceColumn("INTEGER"),
        "d": SourceColumn("DECIMAL(18,6)"),
        "e": SourceColumn("DECIMAL(18,6)"),
        "f": SourceColumn("DOUBLE"),
        "g": SourceColumn("DOUBLE"),
    },
    "r": {
        "id": SourceColumn("INTEGER", nullable=False),
        "n": SourceColumn("INTEGER"),
        "m": SourceColumn("INTEGER"),
    },
}
# 1/3 > 0.333333333333333333 exactly, never as a DOUBLE (0.3333333333333333 is not).
T_ROWS = [
    ["1", "1", "3", "1.000000", "3.000000", "1.0", "3.0"],
    ["2", "2", "3", "2.000000", "3.000000", "2.0", "3.0"],
    ["3", "1", "4", "1.000000", "4.000000", "1.0", "4.0"],
]
# Half ties of each sign: -5/2, 5/-2, -15/-6, -7/2 (#426 rounding parity).
R_ROWS = [["1", "-5", "2"], ["2", "5", "-2"], ["3", "-15", "-6"], ["4", "-7", "2"]]
ADDITIVE = {"element": "metric", "additivity": "additive"}


def metric(name: str, sql: str, **payload: Any) -> Document:  # noqa: ANN401
    declared = ADDITIVE | payload
    return {"name": name, "expression": {"sql": sql}, "custom_extensions": [extension(declared)]}


def derived(name: str, text: str) -> Document:
    over = {"expression": text, "align": {"grain": ["month"], "time": "month"}}
    declared = ADDITIVE | {"over_metrics": over}
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": VENDORS[0], "expression": text}]},
        "custom_extensions": [extension(declared)],
    }


def ratio(name: str, mode: str) -> Document:
    return metric(
        name,
        "SUM(r.n) / SUM(r.m)",
        additivity="recomputable",
        components={"numerator": "SUM(r.n)", "denominator": "SUM(r.m)"},
        zero_denominator="null",
        rounding={"scale": 0, "mode": mode},
    )


def model_document() -> Document:
    fields = {
        "id": "id",
        "q": "a / b",  # an exact quotient no field can declare a rounding for
        "fq": "f / g",  # a float quotient, with no zero-denominator policy to declare
        "fp": "f * g",
        "dd": "d + e",
    }
    document = {
        "version": "0.2.0.dev0",
        "name": "exact",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": ["id"],
                "fields": [{"name": n, "expression": {"sql": e}} for n, e in fields.items()],
            },
            {
                "name": "r",
                "source": "r",
                "primary_key": ["id"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in ("id", "n", "m")],
            },
        ],
        "metrics": [
            metric("thirds", "SUM(CASE WHEN t.q > 0.333333333333333333 THEN 1 ELSE 0 END)"),
            metric("s", "SUM(t.q)"),
            metric("filtered", "COUNT(t.id) FILTER (WHERE t.q > 0.333333333333333333)"),
            derived("twice", "thirds * 2"),
            metric("float_quotients", "SUM(t.fq)"),
            metric("float_products", "SUM(t.fp)"),
            metric("float_halves", "SUM(t.fp) / 2", zero_denominator="null"),
            metric("decimals", "SUM(t.dd)"),
            ratio("even", "half_even"),
            ratio("away", "half_away_from_zero"),
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    return document


def compiled_of(document: Document) -> CompiledModel:
    loaded = read(json.dumps(document).encode(), "semantic/exact.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


MODEL = compiled_of(model_document())


def policies(document: Mapping[str, Any] | None = None) -> PolicyModel:
    return read_policies(
        yaml.safe_dump(dict(document or {"version": 1})),
        MODEL,
        groups=frozenset({"staff"}),
        managed_tables={},
    )


@contextmanager
def lake(folder: Path) -> Iterator[Lake]:
    inputs = []
    for name, rows in (("t", T_ROWS), ("r", R_ROWS)):
        path = folder / f"{name}.csv"
        path.write_text(
            "\n".join([",".join(CATALOG[name]), *(",".join(r) for r in rows)]) + "\n", "utf-8"
        )
        inputs.append(path)
    with ce.case_lake(inputs, CATALOG) as opened:
        yield opened


def aggregate(*metrics: str, group_by: tuple[str, ...] = (), **more: Any) -> Document:  # noqa: ANN401
    document: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
        **more,
    }
    if group_by:
        document["group_by"] = [{"member": g} for g in group_by]
        document["order"] = [{"by": g} for g in group_by]
    return document


def detail(*fields: str, **more: Any) -> Document:  # noqa: ANN401
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": f} for f in fields],
        "order": [{"by": "t.id", "direction": "asc"}],
        "page": {"size": "10"},
        **more,
    }


def never_read(*_: object, **__: object) -> object:
    raise AssertionError("a refused question read data")


def rows_of(found: Any) -> list[list[str | None]]:  # noqa: ANN401
    return [list(r) for r in ce.rows_of(found)]


def builder() -> dict[str, Any]:
    return {**ce.viewer_context(), "role": "builder"}


# (question, the member a Viewer is told about): a Builder is told the member lacking it.
REFUSED = {
    "a metric reading the quotient": (aggregate("thirds"), ("metric", "thirds")),
    "the sum of the quotient": (aggregate("s"), ("metric", "s")),
    "the quotient only in a metric's filter": (aggregate("filtered"), ("metric", "filtered")),
    "a metric over that metric": (aggregate("twice"), ("metric", "twice")),
    "the quotient only as a sort": (
        detail("t.id", order=[{"by": "t.q", "direction": "asc"}]),
        ("field", "t.q"),
    ),
    "the quotient as a detail field": (detail("t.id", "t.q"), ("field", "t.q")),
}


@pytest.mark.parametrize("case", REFUSED.values(), ids=list(REFUSED))
@pytest.mark.parametrize("audience", ["viewer", "builder"])
def test_an_undeclared_exact_quotient_anywhere_it_is_read_is_refused_before_any_read(
    tmp_path: Path, case: tuple[Document, tuple[str, str]], audience: str
) -> None:
    document, (element, name) = case
    context = builder() if audience == "builder" else ce.viewer_context()
    with lake(tmp_path) as opened:
        engine = ce.engine_for(opened, read=never_read)
        with pytest.raises(SemanticError) as caught:
            answer(json.dumps(document), MODEL, policies(), context, engine)
    assert caught.value.code == "MISSING_EXTENSION_DECLARATION"
    expected = ("field", "t.q") if audience == "builder" else (element, name)
    assert dict(caught.value.details) == {
        "element": expected[0],
        "name": expected[1],
        "declaration": "rounding",
    }


@pytest.mark.parametrize("audience", ["viewer", "builder"])
def test_explain_refuses_it_with_the_same_error(tmp_path: Path, audience: str) -> None:
    context = builder() if audience == "builder" else ce.viewer_context()
    with lake(tmp_path) as opened:
        engine = ce.engine_for(opened, read=never_read)
        with pytest.raises(SemanticError) as caught:
            explain(json.dumps(aggregate("twice")), MODEL, policies(), context, engine, None)
    assert caught.value.code == "MISSING_EXTENSION_DECLARATION"
    assert dict(caught.value.details)["name"] == ("t.q" if audience == "builder" else "twice")


HIDDEN = {"version": 1, "column_policies": [{"member": "t.q", "default": "hidden"}]}


@pytest.mark.parametrize("asked", ["thirds", "twice", "s", "filtered"])
@pytest.mark.parametrize("path", ["answer", "explain"])
def test_a_hidden_quotient_hides_what_reads_it_and_the_error_names_only_the_asked_member(
    tmp_path: Path, asked: str, path: str
) -> None:
    with lake(tmp_path) as opened:
        engine = ce.engine_for(opened, read=never_read)
        run = answer if path == "answer" else explain
        extra = () if path == "answer" else (None,)
        with pytest.raises(SemanticError) as caught:
            run(
                json.dumps(aggregate(asked)),
                MODEL,
                policies(HIDDEN),
                ce.viewer_context(),
                engine,
                *extra,
            )
    assert caught.value.code == "MEMBER_NOT_FOUND"
    for audience in ("other", "builder"):
        shown = caught.value.to_message("es", audience).model_dump_json()
        for disclosed in ("rounding", "t.q", '"q"'):
            assert disclosed not in shown


def test_a_float_quotient_without_its_zero_denominator_policy_is_refused_like_any_member(
    tmp_path: Path,
) -> None:
    # Specification correction recorded on the PR: `f / g` divides by zero as `inf` on one
    # engine; a field cannot declare `zero_denominator`, so the lineage rule refuses it too.
    with lake(tmp_path) as opened:
        engine = ce.engine_for(opened, read=never_read)
        with pytest.raises(SemanticError) as caught:
            answer(
                json.dumps(aggregate("float_quotients")),
                MODEL,
                policies(),
                ce.viewer_context(),
                engine,
            )
    assert caught.value.code == "MISSING_EXTENSION_DECLARATION"
    assert dict(caught.value.details)["declaration"] == "zero_denominator"


def test_floats_and_exact_sums_still_answer(tmp_path: Path) -> None:
    with lake(tmp_path) as opened:
        found = answer(
            json.dumps(aggregate("float_products", "decimals", "float_halves")),
            MODEL,
            policies(),
            ce.viewer_context(),
            ce.engine_for(opened),
        )
    columns = {c["name"]: c for c in found.result.envelope()["columns"]}
    assert columns["decimals"]["exactness"] == "exact"
    assert columns["float_products"]["exactness"] != "exact"
    assert rows_of(found)[0][1] == "14.000000"
    assert columns["float_halves"]["exactness"] != "exact"  # a float `/` a bare literal
    assert float(rows_of(found)[0][2] or "nan") == 6.5


# Scale 0 of each half tie: the declared mode, on every sign (#426), and `half_even` equal to the
# engine's own exact division (the oracle measured in the PR).
ROUNDED = {
    "even": [["1", "-2"], ["2", "-2"], ["3", "2"], ["4", "-4"]],
    "away": [["1", "-3"], ["2", "-3"], ["3", "3"], ["4", "-4"]],
}


@pytest.mark.parametrize("name", list(ROUNDED))
def test_an_exact_ratio_rounds_each_negative_half_tie_as_declared(
    tmp_path: Path, name: str
) -> None:
    with lake(tmp_path) as opened:
        found = answer(
            json.dumps(aggregate(name, group_by=("r.id",))),
            MODEL,
            policies(),
            ce.viewer_context(),
            ce.engine_for(opened),
        )
    assert rows_of(found) == ROUNDED[name]


def test_half_even_is_the_engine_s_own_exact_division_on_every_tie() -> None:
    division = "decimal_division(CAST(n AS DECIMAL(9,0)), CAST(m AS DECIMAL(9,0)), 0)"
    values = ", ".join(f"({n}, {m})" for _, n, m in R_ROWS)
    measured = (
        duckdb.connect().execute(f"SELECT {division} FROM (VALUES {values}) v(n, m)").fetchall()
    )
    assert [str(v) for (v,) in measured] == [r[1] for r in ROUNDED["even"]]


def plan_of(document: Document) -> Any:  # noqa: ANN401
    bound = bind_query(
        MODEL,
        json.dumps(document),
        instant=ce.INSTANT,
        limits=QueryLimits(detail_page_size=1000),
        hidden=lambda _: False,
    )
    return plan(analyze(bound, MODEL, policies(), ce.viewer_context()))


def tree_of(name: str) -> Any:  # noqa: ANN401
    return MODEL.require(MemberId("field", "T", name.upper())).parsed.tree


def test_the_duckdb_writer_never_writes_an_exact_slash_even_if_asked_directly() -> None:
    writer = duckdb_adapter._Writer(plan_of(aggregate("decimals")))
    with pytest.raises(SemanticError) as caught:
        writer.node(tree_of("q"), "t0", "T", "field")  # an exact quotient: C2's division only
    assert caught.value.code == "UNSUPPORTED_OPERATION"
    assert " / " in writer.node(tree_of("fq"), "t0", "T", "field")  # a float's own `/`


def test_no_exact_question_s_statement_divides_with_a_slash() -> None:
    for document in (aggregate("decimals"), aggregate("even", group_by=("r.id",))):
        statement = duckdb_adapter.compile_plan(plan_of(document)).sql
        assert re.search(r"[^/]/[^/*]", statement) is None, statement


def compiled_with(sql: str) -> CompiledModel:
    """The model with one more field, `t.extra`, summed by the metric `extra_total`."""
    document = model_document()
    document["datasets"][0]["fields"].append({"name": "extra", "expression": {"sql": sql}})
    document["metrics"].append(metric("extra_total", "SUM(t.extra)"))
    return compiled_of(document)


# Every implicit way an exact value could become a float, each refused when the model compiles:
# no implicit cast between exact and approximate numbers (#280 §2).
MIXED = {
    "exact plus float": "a + f",
    "exact times float": "d * g",
    "exact minus float": "a - f",
    "exact divided by float": "d / g",
    "exact modulo float": "a % f",
    "exact compared with float": "CASE WHEN d > f THEN 1 ELSE 0 END",
    "a CASE mixing exact and float": "CASE WHEN a > 0 THEN d ELSE f END",
    "a COALESCE mixing exact and float": "COALESCE(d, f)",
    "a NULLIF mixing exact and float": "NULLIF(d, f)",
    "a literal past 38 digits": "d + 0.123456789012345678901234567890123456789",
    "an integer literal past 38 digits": "a + 123456789012345678901234567890123456789",
}


@pytest.mark.parametrize("sql", MIXED.values(), ids=list(MIXED))
def test_an_implicit_exact_to_float_conversion_is_refused_when_the_model_compiles(
    sql: str,
) -> None:
    with pytest.raises(SemanticError) as caught:
        compiled_with(sql)
    assert caught.value.code == "OSSIE_INVALID"
    problems = caught.value.details["problems"]
    assert isinstance(problems, list)
    assert [p["rule"] for p in problems] == ["type"]


def ask_extra(tmp_path: Path, sql: str, read: Any = None) -> Any:  # noqa: ANN401
    compiled = compiled_with(sql)
    policy = read_policies(
        yaml.safe_dump({"version": 1}), compiled, groups=frozenset(), managed_tables={}
    )
    with lake(tmp_path) as opened:
        engine = ce.engine_for(opened, **({"read": read} if read else {}))
        return answer(
            json.dumps(aggregate("extra_total")), compiled, policy, ce.viewer_context(), engine
        )


def test_a_literal_the_engine_would_read_as_a_float_is_refused_before_any_read(
    tmp_path: Path,
) -> None:
    with pytest.raises(SemanticError) as caught:
        ask_extra(tmp_path, "d * 1e3", read=never_read)  # DuckDB reads `1e3` as a DOUBLE
    assert caught.value.code == "UNSUPPORTED_OPERATION"


def test_an_explicit_float_cast_is_refused_before_any_read_as_no_dialect_writes_it(
    tmp_path: Path,
) -> None:
    # Specification correction recorded on the PR: no dialect writes `CAST(… AS DOUBLE)` yet,
    # so the only way to a float is a declared float column; the cast stays refused.
    with pytest.raises(SemanticError) as caught:
        ask_extra(tmp_path, "CAST(d AS DOUBLE) * f", read=never_read)
    assert caught.value.code == "UNSUPPORTED_OPERATION"


# One field per node kind the DuckDB writer writes (Ref, Number, String, Boolean, Null, Unary,
# Binary, Not, Between, InList, IsNull, Case, Cast, Call), all over exact or text columns.
NODE_KINDS = {
    "Number": "d + 1.5",
    "String": "CASE WHEN 'x' = 'x' THEN d ELSE e END",
    "Boolean": "CASE WHEN TRUE THEN d ELSE e END",
    "Null": "COALESCE(NULL, d)",
    "Unary": "-d",
    "Binary": "d * e - a % b",
    "Not": "CASE WHEN NOT (a > b) THEN d ELSE e END",
    "Between": "CASE WHEN a BETWEEN 1 AND 2 THEN d ELSE e END",
    "InList": "CASE WHEN a IN (1, 2) THEN d ELSE e END",
    "IsNull": "CASE WHEN d IS NULL THEN e ELSE d END",
    "Case": "CASE WHEN a > b THEN d ELSE e END",
    "Cast": "CAST(d AS DECIMAL(20,2))",
    "Call": "ABS(NULLIF(d, e))",
}


def kinds_in(tree: Any) -> set[str]:  # noqa: ANN401
    found = {type(tree).__name__}
    for value in vars(tree).values() if hasattr(tree, "__dict__") else ():
        found |= _kinds(value)
    for slot in getattr(type(tree), "__slots__", ()):
        found |= _kinds(getattr(tree, slot))
    return found


def _kinds(value: Any) -> set[str]:  # noqa: ANN401
    if isinstance(value, tuple):
        return set().union(*(_kinds(v) for v in value)) if value else set()
    if hasattr(value, "__slots__") or hasattr(value, "__dict__"):
        return kinds_in(value)
    return set()


def test_every_node_kind_the_duckdb_writer_writes_is_written_without_an_exact_slash() -> None:
    source = Path(duckdb_adapter.__file__).read_text("utf-8")
    source += Path(duckdb_adapter.__file__).with_name("dialect.py").read_text("utf-8")
    handled = set(re.findall(r"isinstance\(node, sql\.(\w+)\)", source))
    writer = duckdb_adapter._Writer(plan_of(aggregate("decimals")))
    covered: set[str] = set()
    refusals: list[str] = []
    for sql in NODE_KINDS.values():
        compiled = compiled_with(sql)
        tree = compiled.require(MemberId("field", "T", "EXTRA")).parsed.tree
        assert tree is not None
        covered |= kinds_in(tree)
        writer.adopt(compiled)
        try:
            written = writer.node(tree, "t0", "T", "field")
        except SemanticError as refused:  # a kind the writer refuses here writes nothing
            refusals.append(refused.code)
            continue
        assert re.search(r"[^/]/[^/*]", written) is None, written
    assert set(refusals) <= {"UNSUPPORTED_OPERATION"}
    assert handled <= covered | {"Ref"}, handled - covered


def test_an_exact_mean_needs_its_declarations_and_no_dialect_writes_it(tmp_path: Path) -> None:
    document = model_document()
    document["metrics"].append(metric("mean", "AVG(t.dd)", additivity="non_additive"))
    with pytest.raises(SemanticError) as caught:
        compiled_of(document)  # refused when the model compiles: the mean's scale is undeclared
    assert caught.value.code == "MISSING_EXTENSION_DECLARATION"
    document["metrics"][-1] = metric(
        "mean",
        "AVG(t.dd)",
        additivity="non_additive",
        zero_denominator="null",
        rounding={"scale": 2, "mode": "half_even"},
    )
    compiled = compiled_of(document)
    policy = read_policies(
        yaml.safe_dump({"version": 1}), compiled, groups=frozenset(), managed_tables={}
    )
    with lake(tmp_path) as opened, pytest.raises(SemanticError) as refused:
        answer(
            json.dumps(aggregate("mean")),
            compiled,
            policy,
            ce.viewer_context(),
            ce.engine_for(opened, read=never_read),
        )
    assert refused.value.code == "UNSUPPORTED_OPERATION"  # never the engine's float mean
