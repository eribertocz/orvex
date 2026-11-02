"""Type every parsed expression (P2-04c, #289; #280 is the source of truth).

`type_model(model, parsed, catalog)` returns a `TypedModel` or raises `SemanticError`.

Every node of every compiled expression carries a `NodeType`: its logical type (Ossie's
`datatype` with the exact numeric shape, and a zone on `DateTime` and `DateTimeTz`), semantic
type, nullability, exactness, kind (`row`, `aggregate`, `metric`), aggregate class (the most
restrictive below it) and volatility. The rules are the engine's own, never a dialect's
(#280 agent decision 3); adapters reproduce them or reject (P2-12, P2-13, P2-31).

**Rules (fail closed):**
- no implicit cast between type families, and none between exact and approximate numbers (a
  bare number literal takes its partner's exactness); `DateTime` and `DateTimeTz` never
  compare; a string literal compares with a temporal value only when it is an ISO 8601 text of
  that type (an instant only with an explicit offset);
- exact decimals: `+`/`-` → scale max(s1,s2), precision max(p1-s1,p2-s2)+scale+1; `*` →
  p1+p2, s1+s2; `SUM` → (38,s); integers mix as Decimal(digits,0), and integer arithmetic and
  `SUM` of integers stay integers (`SUM` → signed 128-bit). A precision above 38 is capped at
  38 with an overflow obligation on that node; a scale above 38 is rejected. `/`, `AVG`,
  `MEDIAN` and `PERCENTILE_CONT` over exact inputs take the metric's `rounding` scale (undecided
  without it: P2-04e records the missing declaration). A quotient, a mean or a narrowing `CAST`
  that may exceed its type carries the overflow obligation too;
- a calendar operation on an instant applies the bucketing field's own `time_zone`, then the
  bucketed value's, then the model's `default_time_zone`;
- approximation only when explicit: `CAST … AS DOUBLE`/`FLOAT`, `APPROX_*`, `STDDEV` and
  `VARIANCE`, `SQRT`/`LN`/`EXP`/`POWER`/`LOG`/trigonometry/`PI`, or a `Float` column. A
  number literal is exact, exponent included;
- a declared `datatype` (and `numeric`) equals the inferred one;
- `identifier`, `category`, `geo_region`, `latitude` and `longitude` admit no arithmetic, `SUM`
  or `AVG`; `ratio` admits no `SUM`;
- a field is row-level and a metric aggregate; no aggregate inside an aggregate; a row value
  beside an aggregate is refused; `FILTER` is a row-level boolean over the aggregated dataset;
- an engine-dialect (opaque) expression is typed from its declarations, and a metric's is
  `holistic`;
- zones exist in the pinned `tzdata` package (`zones.py`).

**Errors** (`OSSIE_INVALID`, pointer and line as P2-03 reports them): `type` at the expression
text (at `datatype` for a declared type that differs; at the payload for `numeric`, payload
expressions and declarations), `reference` for a source column the catalog does not hold,
`extension` for an unknown zone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Literal, TypedDict

from orvex.semantic.compiler.catalog import (
    MAX_PRECISION,
    LogicalType,
    SourceCatalog,
    SourceColumn,
    column_key,
    integer_digits,
    logical_of,
)
from orvex.semantic.compiler.parsing import MemberId, ParsedMember, ParsedModel, Problems
from orvex.semantic.compiler.units import ONE, Unit, UnitError
from orvex.semantic.compiler.units import currency as currency_unit
from orvex.semantic.compiler.units import parse as parse_unit
from orvex.semantic.compiler.zones import TZDATA_VERSION, is_zone
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.model import OssieModel

Kind = Literal["row", "aggregate", "metric"]
Exactness = Literal["exact", "approximate", "unknown"]  # unknown: an undeclared opaque type
Volatility = Literal["immutable", "query_instant"]
Json = Mapping[str, object]

NUMERIC = frozenset({"Integer", "Decimal", "Float"})
DATED = frozenset({"Date", "DateTime", "DateTimeTz"})
TIMED = frozenset({"Time", "DateTime", "DateTimeTz"})
TEMPORAL = DATED | TIMED
STRING = frozenset({"String"})
BOOLEAN = frozenset({"Boolean"})
ORDERED = NUMERIC | TEMPORAL | STRING | BOOLEAN

NO_ARITHMETIC = frozenset({"identifier", "category", "geo_region", "latitude", "longitude"})
NO_SUM = NO_ARITHMETIC | {"ratio"}
NO_AVERAGE = NO_ARITHMETIC

APPROXIMATING = frozenset(
    """
    STDDEV STDDEV_POP STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP POWER SQRT EXP LN LOG LOG10 SIN COS
    TAN ASIN ACOS ATAN ATAN2 RADIANS DEGREES PI
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)


@dataclass(frozen=True, slots=True)
class NodeType:
    logical: LogicalType
    semantic_type: str | None
    nullable: bool
    exactness: Exactness
    kind: Kind
    aggregate_class: sql.AggregateClass | None
    volatility: Volatility
    overflow_check: bool  # adapter obligation at this node: the value may exceed its type
    constant: bool  # literals only: no column, aggregate or metric below
    bucket_zone: str | None = None  # the zone a calendar operation on an instant applies
    buckets_instant: bool = False  # a calendar operation on a DateTimeTz value
    unit: Unit | None = None  # None: undeclared (P2-04d)


@dataclass(frozen=True, slots=True)
class TypedNode:
    node: sql.Node
    type: NodeType
    children: tuple[TypedNode, ...]


@dataclass(frozen=True)
class TypedMember:
    id: MemberId
    type: NodeType
    tree: TypedNode | None  # None for an engine-dialect expression
    payload: tuple[tuple[str, TypedNode], ...]  # (`calculated`, `components.numerator`, …)


@dataclass(frozen=True)
class TypedModel:
    parsed: ParsedModel
    members: Mapping[MemberId, TypedMember]
    expressions: tuple[tuple[str, TypedNode], ...]  # model payload expressions, by pointer
    tzdata: str


class _Extra(TypedDict, total=False):
    semantic: str | None
    nullable: bool
    approximate: bool
    volatile: bool
    overflow: bool
    bucket_zone: str | None
    buckets_instant: bool


class _Bad(Exception):
    def __init__(self, rule: str = "type", pointer: str | None = None) -> None:
        super().__init__(rule)
        self.rule = rule
        self.pointer = pointer


class _Skip(Exception):
    """A member this one reads was refused; its own error is already reported."""


# --- small helpers ----------------------------------------------------------------------------


def _family(datatype: str | None) -> str | None:
    return "numeric" if datatype in NUMERIC else datatype


def _cap(precision: int) -> tuple[int, bool]:
    return (MAX_PRECISION, True) if precision > MAX_PRECISION else (precision, False)


def _decimal(logical: LogicalType) -> tuple[int | None, int | None]:
    """An exact number as (precision, scale); integers mix as Decimal(digits, 0)."""
    if logical.datatype == "Integer":
        if logical.width is None:
            return None, 0
        return integer_digits(logical.width, logical.signed is not False), 0
    return logical.precision, logical.scale


def _exceeds(
    precision: int | None, scale: int | None, extra: int | None, rounding: int | None
) -> bool:
    """True when a quotient or a mean of that shape may need more than 38 digits: its integer
    digits (precision - scale + extra) plus the rounding scale; unknown shapes may."""
    if precision is None or scale is None or extra is None or rounding is None:
        return True
    return precision - scale + extra + rounding > MAX_PRECISION


def _bounds(logical: LogicalType, scale: int) -> tuple[Fraction, Fraction] | None:
    """The smallest and largest value of an exact shape once rounded to `scale` digits; None
    when the shape is unknown. Rounding 9.99 to one digit reaches 10.0."""
    if logical.datatype == "Integer":
        if logical.width is None or logical.signed is None:
            return None
        if logical.signed:
            return Fraction(-(2 ** (logical.width - 1))), Fraction(2 ** (logical.width - 1) - 1)
        return Fraction(0), Fraction(2**logical.width - 1)
    if logical.precision is None or logical.scale is None:
        return None
    if scale < logical.scale:
        largest = Fraction(10 ** (logical.precision - logical.scale))
    else:
        largest = Fraction(10**logical.precision - 1, 10**logical.scale)
    return -largest, largest


def _narrows(source: LogicalType, target: LogicalType) -> bool:
    """True when a CAST to an exact type may receive a value its target cannot hold (#280 §2:
    overflow is an obligation, never wraparound). Text, floats and unknown shapes may."""
    if target.datatype not in ("Integer", "Decimal"):
        return False
    if source.datatype not in ("Integer", "Decimal"):
        return source.datatype not in ("Boolean", "Date", "Time", "DateTime", "DateTimeTz")
    scale = target.scale if target.datatype == "Decimal" and target.scale is not None else 0
    held, holds = _bounds(source, scale), _bounds(target, scale)
    if held is None or holds is None:
        return True
    return held[0] < holds[0] or held[1] > holds[1]


def _decimal_type(precision: int | None, scale: int | None) -> tuple[LogicalType, bool]:
    if scale is not None and scale > MAX_PRECISION:
        raise _Bad()
    if precision is None or scale is None:
        return LogicalType(
            "Decimal", MAX_PRECISION if precision is None else min(precision, 38), scale
        ), False
    capped, overflow = _cap(max(precision, scale, 1))
    return LogicalType("Decimal", capped, scale), overflow


def _widest_integer(types: Iterable[LogicalType]) -> LogicalType:
    widths = [t.width for t in types]
    width = None if None in widths else max(w for w in widths if w is not None)
    return LogicalType("Integer", width=width, signed=True)


def _literal_number(text: str) -> LogicalType:
    """The exact type of a number as written. A literal is exact, exponent included (`1e3` is
    1000): approximation comes only from the closed list of #280 §2."""
    value = Decimal(text)
    _, digits, exponent = value.as_tuple()  # the scale as written: `2.50` is Decimal(3,2)
    assert isinstance(exponent, int)
    scale = max(-exponent, 0)
    precision = max(len(digits) + max(exponent, 0), scale, 1)
    if scale > MAX_PRECISION or precision > MAX_PRECISION:
        raise _Bad()
    if scale == 0 and abs(value) < 2**63:
        return LogicalType("Integer", width=64, signed=True)
    return LogicalType("Decimal", precision, scale)


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?"
    r"(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})?"
)
_TIME = re.compile(r"[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?")


def _is_temporal_text(text: str, datatype: str) -> bool:
    """An ISO 8601 text of exactly that temporal type."""
    try:
        if datatype == "Date":
            return _DATE.fullmatch(text) is not None and bool(date.fromisoformat(text))
        if datatype == "Time":
            return _TIME.fullmatch(text) is not None and bool(time.fromisoformat(text))
        matched = _DATETIME.fullmatch(text)
        if matched is None:
            return datatype == "DateTime" and _is_temporal_text(text, "Date")
        has_offset = matched.group("offset") is not None
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return has_offset == (datatype == "DateTimeTz")


# --- the checker ------------------------------------------------------------------------------

Resolver = Callable[[sql.Ref], NodeType]


@dataclass(frozen=True)
class _Context:
    resolve: Resolver
    rounding: int | None  # the metric's `rounding.scale`
    zone: str | None  # the bucketing field's own declared zone (field expressions only)
    dataset_of: Callable[[sql.Ref], str | None]  # the dataset a reference reads, for FILTER
    default_zone: str | None = None  # the model's `default_time_zone`


def _merge(
    children: Sequence[TypedNode],
    logical: LogicalType,
    *,
    semantic: str | None = None,
    nullable: bool | None = None,
    approximate: bool = False,
    exact: bool = False,
    kind: Kind | None = None,
    aggregate: sql.AggregateClass | None = None,
    volatile: bool = False,
    overflow: bool = False,
    bucket_zone: str | None = None,
    buckets_instant: bool = False,
    constant: bool | None = None,
) -> NodeType:
    types = [child.type for child in children]
    kinds = {t.kind for t in types if not t.constant}
    if "row" in kinds and len(kinds) > 1:
        raise _Bad()  # a row value beside an aggregate or a metric
    if kind is None:
        kind = "metric" if "metric" in kinds else "aggregate" if "aggregate" in kinds else "row"
    classes = [t.aggregate_class for t in types if t.aggregate_class is not None]
    if aggregate is not None:
        classes.append(aggregate)
    return NodeType(
        logical=logical,
        semantic_type=semantic,
        nullable=any(t.nullable for t in types) if nullable is None else nullable,
        exactness=_exactness(types, logical, approximate=approximate, exact=exact),
        kind=kind,
        aggregate_class=max(classes, key=sql.AGGREGATE_CLASSES.index) if classes else None,
        volatility="query_instant"
        if volatile or any(t.volatility == "query_instant" for t in types)
        else "immutable",
        overflow_check=overflow,
        constant=(not kinds and aggregate is None and kind == "row")
        if constant is None
        else constant,
        bucket_zone=bucket_zone,
        buckets_instant=buckets_instant,
    )


def _exactness(
    types: Sequence[NodeType], logical: LogicalType, *, approximate: bool, exact: bool
) -> Exactness:
    if exact:
        return "exact"
    if approximate or any(t.exactness == "approximate" for t in types):
        return "approximate"
    if any(t.exactness == "unknown" for t in types):
        return "unknown"
    return "exact"


def _need(child: TypedNode, allowed: frozenset[str]) -> None:
    datatype = child.type.logical.datatype
    if datatype is not None and datatype not in allowed:
        raise _Bad()


def _is_null(child: TypedNode) -> bool:
    return isinstance(child.node, sql.Null)


def _literal(child: TypedNode) -> bool:
    return isinstance(child.node, sql.Number) or (
        isinstance(child.node, sql.Unary) and _literal(child.children[0])
    )


def _comparable(left: TypedNode, right: TypedNode) -> None:
    """Equal families, without an implicit cast (#280 §2)."""
    a, b = left.type.logical.datatype, right.type.logical.datatype
    if a is None or b is None:
        return
    if a == "Opaque" or b == "Opaque":
        raise _Bad()
    for text_side, other in ((left, b), (right, a)):
        if isinstance(text_side.node, sql.String) and other in TEMPORAL:
            if not _is_temporal_text(text_side.node.value, other):
                raise _Bad()
            return
    if _family(a) != _family(b):
        raise _Bad()
    if a in TEMPORAL and a != b:
        raise _Bad()  # DateTime and DateTimeTz never compare; no implicit Date widening
    if (a == "Float") != (b == "Float") and not (_literal(left) or _literal(right)):
        raise _Bad()  # exact against approximate


def _unify(children: Sequence[TypedNode]) -> tuple[LogicalType, bool]:
    """The common type of alternatives (CASE, COALESCE, GREATEST, …), with its overflow
    obligation."""
    known = [c for c in children if c.type.logical.datatype is not None]
    if len(known) < len(children) and any(not _is_null(c) for c in children if c not in known):
        return LogicalType(None), False  # an undeclared opaque type
    if not known:
        return LogicalType(None), False
    for other in known[1:]:
        _comparable(known[0], other)
    types = [c.type.logical for c in known]
    first = types[0]
    if first.datatype not in NUMERIC:
        zones = {t.zone for t in types}
        return replace(first, zone=first.zone if len(zones) == 1 else None), False
    if any(t.datatype == "Float" for t in types):
        return LogicalType("Float"), False
    if all(t.datatype == "Integer" for t in types):
        return _widest_integer(types), False
    shapes = [_decimal(t) for t in types]
    if any(p is None or s is None for p, s in shapes):
        return LogicalType("Decimal", MAX_PRECISION, None), False
    scale = max(s for _, s in shapes if s is not None)
    whole = max(p - s for p, s in shapes if p is not None and s is not None)
    return _decimal_type(whole + scale, scale)


def _semantic(children: Sequence[TypedNode]) -> str | None:
    """The semantic type of alternatives: theirs when they agree; otherwise the most
    restrictive among them, so that a restriction never disappears by mixing."""
    kinds = {c.type.semantic_type for c in children if not c.type.constant}
    if len(kinds) == 1:
        return kinds.pop()
    named = {kind for kind in kinds if kind is not None}
    for restricted in (sorted(named & NO_ARITHMETIC), sorted(named & NO_SUM)):
        if restricted:
            return restricted[0]
    return None


class _Checker:
    def __init__(self, context: _Context) -> None:
        self.context = context

    def check(self, node: sql.Node) -> TypedNode:
        handler = getattr(self, f"_{type(node).__name__.lower()}")
        typed: TypedNode = handler(node)
        return typed

    def _leaf(self, node: sql.Node, logical: LogicalType, *, nullable: bool = False) -> TypedNode:
        return TypedNode(node, _merge((), logical, nullable=nullable), ())

    # literals and references

    def _number(self, node: sql.Number) -> TypedNode:
        return TypedNode(node, _merge((), _literal_number(node.text), nullable=False), ())

    def _string(self, node: sql.String) -> TypedNode:
        return self._leaf(node, LogicalType("String"))

    def _boolean(self, node: sql.Boolean) -> TypedNode:
        return self._leaf(node, LogicalType("Boolean"))

    def _null(self, node: sql.Null) -> TypedNode:
        return self._leaf(node, LogicalType(None), nullable=True)

    def _ref(self, node: sql.Ref) -> TypedNode:
        return TypedNode(node, self.context.resolve(node), ())

    def _star(self, node: sql.Star) -> TypedNode:
        raise _Bad()  # only COUNT(*), handled there

    def _datepart(self, node: sql.DatePart) -> TypedNode:
        raise _Bad()  # only as a function's first argument, handled there

    # operators

    def _unary(self, node: sql.Unary) -> TypedNode:
        operand = self.check(node.operand)
        _need(operand, NUMERIC)
        if node.op == "-" and operand.type.semantic_type in NO_ARITHMETIC:
            raise _Bad()
        logical = operand.type.logical
        overflow = node.op == "-" and logical.datatype == "Integer"
        if overflow:
            logical = LogicalType("Integer", width=logical.width, signed=True)
        semantic = operand.type.semantic_type
        return TypedNode(
            node, _merge((operand,), logical, semantic=semantic, overflow=overflow), (operand,)
        )

    def _binary(self, node: sql.Binary) -> TypedNode:
        left, right = self.check(node.left), self.check(node.right)
        children = (left, right)
        if node.op in ("AND", "OR"):
            _need(left, BOOLEAN)
            _need(right, BOOLEAN)
            return TypedNode(node, _merge(children, LogicalType("Boolean")), children)
        if node.op in ("=", "<>", "<", ">", "<=", ">="):
            _comparable(left, right)
            return TypedNode(node, _merge(children, LogicalType("Boolean")), children)
        if node.op == "||":
            _need(left, STRING)
            _need(right, STRING)
            return TypedNode(node, _merge(children, LogicalType("String")), children)
        return self._arithmetic(node, left, right)

    def _arithmetic(self, node: sql.Binary, left: TypedNode, right: TypedNode) -> TypedNode:
        children = (left, right)
        for side in children:
            _need(side, NUMERIC)
            if side.type.semantic_type in NO_ARITHMETIC:
                raise _Bad()
        a, b = left.type.logical, right.type.logical
        semantic = self._arithmetic_semantic(node.op, left, right)
        if a.datatype is None or b.datatype is None:
            unknown = not (_is_null(left) or _is_null(right))
            logical = LogicalType(None) if unknown else (b if a.datatype is None else a)
            return TypedNode(node, _merge(children, logical, semantic=semantic), children)
        if a.datatype == "Float" or b.datatype == "Float":
            if not (a.datatype == b.datatype or _literal(left) or _literal(right)):
                raise _Bad()  # exact with approximate
            return TypedNode(
                node, _merge(children, LogicalType("Float"), semantic=semantic), children
            )
        overflow = False
        nullable: bool | None = None
        if node.op == "/":
            # The quotient needs p1 - s1 + s2 integer digits plus the rounding scale.
            (p1, s1), (_, s2) = _decimal(a), _decimal(b)
            logical, _ = _decimal_type(MAX_PRECISION, self.context.rounding)
            overflow = _exceeds(p1, s1, s2, self.context.rounding)
            nullable = True  # a zero denominator follows `zero_denominator`
        elif a.datatype == "Integer" and b.datatype == "Integer":
            logical, overflow = _widest_integer((a, b)), True
        else:
            (p1, s1), (p2, s2) = _decimal(a), _decimal(b)
            if p1 is None or s1 is None or p2 is None or s2 is None:
                if s1 is None or s2 is None:
                    scale = None
                else:
                    scale = s1 + s2 if node.op == "*" else max(s1, s2)
                logical, _ = _decimal_type(None, scale)
                overflow = True
            else:
                if node.op == "*":
                    logical, overflow = _decimal_type(p1 + p2, s1 + s2)
                else:
                    scale = max(s1, s2)
                    extra = 1 if node.op in ("+", "-") else 0
                    logical, overflow = _decimal_type(max(p1 - s1, p2 - s2) + scale + extra, scale)
        return TypedNode(
            node,
            _merge(children, logical, semantic=semantic, nullable=nullable, overflow=overflow),
            children,
        )

    @staticmethod
    def _arithmetic_semantic(op: str, left: TypedNode, right: TypedNode) -> str | None:
        if _literal(right):
            return left.type.semantic_type
        if _literal(left) and op in ("+", "-", "*"):
            return right.type.semantic_type
        if op in ("+", "-") and left.type.semantic_type == right.type.semantic_type:
            return left.type.semantic_type
        return None

    def _not(self, node: sql.Not) -> TypedNode:
        operand = self.check(node.operand)
        _need(operand, BOOLEAN)
        return TypedNode(node, _merge((operand,), LogicalType("Boolean")), (operand,))

    def _between(self, node: sql.Between) -> TypedNode:
        children = (self.check(node.operand), self.check(node.low), self.check(node.high))
        _comparable(children[0], children[1])
        _comparable(children[0], children[2])
        return TypedNode(node, _merge(children, LogicalType("Boolean")), children)

    def _inlist(self, node: sql.InList) -> TypedNode:
        operand = self.check(node.operand)
        items = tuple(self.check(item) for item in node.items)
        for item in items:
            _comparable(operand, item)
        children = (operand, *items)
        return TypedNode(node, _merge(children, LogicalType("Boolean")), children)

    def _like(self, node: sql.Like) -> TypedNode:
        parts = [node.operand, node.pattern] + ([node.escape] if node.escape is not None else [])
        children = tuple(self.check(part) for part in parts)
        for child in children:
            _need(child, STRING)
        return TypedNode(node, _merge(children, LogicalType("Boolean")), children)

    def _isnull(self, node: sql.IsNull) -> TypedNode:
        operand = self.check(node.operand)
        return TypedNode(
            node, _merge((operand,), LogicalType("Boolean"), nullable=False), (operand,)
        )

    def _isdistinct(self, node: sql.IsDistinct) -> TypedNode:
        children = (self.check(node.left), self.check(node.right))
        _comparable(*children)
        return TypedNode(node, _merge(children, LogicalType("Boolean"), nullable=False), children)

    def _case(self, node: sql.Case) -> TypedNode:
        operand = self.check(node.operand) if node.operand is not None else None
        conditions: list[TypedNode] = []
        results: list[TypedNode] = []
        for when, then in node.whens:
            condition = self.check(when)
            if operand is None:
                _need(condition, BOOLEAN)
            else:
                _comparable(operand, condition)
            conditions.append(condition)
            results.append(self.check(then))
        if node.otherwise is not None:
            results.append(self.check(node.otherwise))
        logical, overflow = _unify(results)
        children = tuple(([operand] if operand is not None else []) + conditions + results)
        nullable = node.otherwise is None or any(r.type.nullable for r in results)
        return TypedNode(
            node,
            _merge(
                children, logical, semantic=_semantic(results), nullable=nullable, overflow=overflow
            ),
            children,
        )

    def _cast(self, node: sql.Cast) -> TypedNode:
        operand = self.check(node.operand)
        name, arguments = node.type_name, node.arguments
        if name in ("DECIMAL", "NUMERIC"):
            if len(arguments) != 2:
                raise _Bad()  # precision and scale are required
            precision, scale = arguments
            if not (1 <= precision <= MAX_PRECISION and 0 <= scale <= precision):
                raise _Bad()
            logical = LogicalType("Decimal", precision, scale)
        elif arguments:
            raise _Bad()
        else:
            logical = {
                "VARCHAR": LogicalType("String"),
                "STRING": LogicalType("String"),
                "INTEGER": LogicalType("Integer", width=32, signed=True),
                "INT": LogicalType("Integer", width=32, signed=True),
                "BIGINT": LogicalType("Integer", width=64, signed=True),
                "FLOAT": LogicalType("Float"),
                "DOUBLE": LogicalType("Float"),
                "BOOLEAN": LogicalType("Boolean"),
                "DATE": LogicalType("Date"),
                "TIMESTAMP": LogicalType("DateTime"),
                "TIME": LogicalType("Time"),
            }[name]
        instant = operand.type.logical.datatype == "DateTimeTz" and logical.datatype in TEMPORAL
        zone = self._zone(operand) if instant else None
        if logical.datatype == "DateTime" and operand.type.logical.datatype == "DateTime":
            logical = replace(logical, zone=operand.type.logical.zone)
        return TypedNode(
            node,
            _merge(
                (operand,),
                logical,
                semantic=operand.type.semantic_type,
                nullable=operand.type.nullable or node.safe,
                approximate=logical.datatype == "Float",
                overflow=_narrows(operand.type.logical, logical),
                bucket_zone=zone,
                buckets_instant=instant,
            ),
            (operand,),
        )

    def _zone(self, operand: TypedNode) -> str | None:
        """The zone a calendar operation on an instant applies, the declaration closest to it
        first: the bucketing field's own `time_zone`, then the bucketed value's, then the
        model's `default_time_zone`; None when nothing declares one (P2-04e records it)."""
        return self.context.zone or operand.type.logical.zone or self.context.default_zone

    def _window(self, node: sql.Window) -> TypedNode:
        raise _Bad()  # excluded by P2-04b (`window_function`); never reaches typing

    # calls

    def _call(self, node: sql.Call) -> TypedNode:
        if node.name in sql.AGGREGATES:
            return self._aggregate(node)
        if node.filter is not None or node.order is not None or node.distinct:
            raise _Bad()
        if node.name in ("DATE_TRUNC", "DATE_PART", "EXTRACT", "DATEADD", "DATEDIFF"):
            if not node.arguments or not isinstance(node.arguments[0], sql.DatePart | sql.String):
                raise _Bad()  # a calendar function names its date part first
            return self._calendar(node)
        arguments = tuple(self.check(argument) for argument in node.arguments)
        return _Scalars.type(node, arguments, self)

    def _part(self, node: sql.Call) -> str:
        first = node.arguments[0]
        part = first.part if isinstance(first, sql.DatePart) else str(getattr(first, "value", ""))
        part = part.upper()
        if part not in sql.DATE_PARTS:
            raise _Bad()
        return part

    def _calendar(self, node: sql.Call) -> TypedNode:
        part = self._part(node)
        rest = tuple(self.check(argument) for argument in node.arguments[1:])
        expected = {"DATE_TRUNC": 1, "DATE_PART": 1, "EXTRACT": 1, "DATEADD": 2, "DATEDIFF": 2}
        if len(rest) != expected[node.name]:
            raise _Bad()
        values = rest[1:] if node.name == "DATEADD" else rest
        if node.name == "DATEADD":
            _need(rest[0], frozenset({"Integer"}))
        if node.name == "DATEDIFF":
            _comparable(rest[0], rest[1])
        allowed = TIMED if part in ("HOUR", "MINUTE", "SECOND", "MILLISECOND") else DATED
        for value in values:
            _need(value, TEMPORAL if node.name == "DATEADD" else allowed)
        target = values[0].type.logical
        instant = target.datatype == "DateTimeTz"
        zone = self._zone(values[0]) if instant else None
        if node.name in ("DATE_TRUNC", "DATEADD"):
            logical = replace(target, zone=zone) if instant else target
        else:
            logical = LogicalType("Integer", width=64, signed=True)
        return TypedNode(
            node,
            _merge(rest, logical, bucket_zone=zone, buckets_instant=instant),
            rest,
        )

    def _aggregate(self, node: sql.Call) -> TypedNode:
        name = node.name
        if node.order is not None:
            if name not in ("PERCENTILE_CONT", "PERCENTILE_DISC"):
                raise _Bad()
            fraction = [self.check(argument) for argument in node.arguments]
            if len(fraction) != 1 or not fraction[0].type.constant:
                raise _Bad()
            _need(fraction[0], NUMERIC)
            values = [self.check(node.order)]
        elif name in ("PERCENTILE_CONT", "PERCENTILE_DISC"):
            raise _Bad()
        elif name == "COUNT" and node.arguments == (sql.Star(),):
            values = []
        else:
            values = [self.check(argument) for argument in node.arguments]
        expected = 2 if name == "APPROX_PERCENTILE" else 1
        if node.order is None and not (name == "COUNT" and not values) and len(values) != expected:
            raise _Bad()
        for value in values:
            if value.type.kind != "row" and not value.type.constant:
                raise _Bad()  # no aggregate inside an aggregate
        children = list(values)
        if node.filter is not None:
            condition = self.check(node.filter)
            _need(condition, BOOLEAN)
            if condition.type.kind != "row" and not condition.type.constant:
                raise _Bad()
            self._filter_scope(node)
            children.append(condition)
        kind = sql.AGGREGATES[name]
        if node.distinct and name not in ("MIN", "MAX"):
            kind = "holistic"
        logical, approximate, exact, overflow, nullable, semantic = self._aggregate_result(
            name, values
        )
        return TypedNode(
            node,
            _merge(
                children,
                logical,
                semantic=semantic,
                nullable=nullable,
                approximate=approximate,
                exact=exact,
                kind="aggregate",
                aggregate=kind,
                overflow=overflow,
            ),
            tuple(children),
        )

    def _filter_scope(self, node: sql.Call) -> None:
        """`FILTER` reads the dataset the aggregate reads (L267)."""
        assert node.filter is not None
        argued = {
            self.context.dataset_of(ref)
            for argument in (*node.arguments, *([node.order] if node.order else []))
            for ref in sql.references(argument)
        }
        filtered = {self.context.dataset_of(ref) for ref in sql.references(node.filter)}
        if argued and not filtered <= argued:
            raise _Bad()
        if len(filtered) > 1:
            raise _Bad()

    def _aggregate_result(
        self, name: str, values: Sequence[TypedNode]
    ) -> tuple[LogicalType, bool, bool, bool, bool, str | None]:
        """(type, approximate, exact, overflow, nullable, semantic type)."""
        if name in ("COUNT", "APPROX_COUNT_DISTINCT"):
            approximate = name == "APPROX_COUNT_DISTINCT"
            return (
                LogicalType("Integer", width=64, signed=True),
                approximate,
                not approximate,
                False,
                False,
                None,
            )
        value = values[0]
        logical = value.type.logical
        semantic = value.type.semantic_type
        if name in ("MIN", "MAX", "PERCENTILE_DISC"):
            _need(value, ORDERED)
            return logical, False, False, False, True, semantic
        if name == "APPROX_PERCENTILE":
            _need(value, NUMERIC | TEMPORAL)
            if not values[1].type.constant:
                raise _Bad()
            _need(values[1], NUMERIC)
            return logical, True, False, False, True, semantic
        _need(value, NUMERIC)
        if name == "SUM":
            if semantic in NO_SUM:
                raise _Bad()
            if logical.datatype == "Integer":
                return (
                    LogicalType("Integer", width=128, signed=True),
                    False,
                    False,
                    True,
                    True,
                    semantic,
                )
            if logical.datatype == "Decimal":
                typed, _ = _decimal_type(None, logical.scale)
                return typed, False, False, True, True, semantic
            return logical, False, False, False, True, semantic
        if semantic in NO_AVERAGE:
            raise _Bad()
        if name in APPROXIMATING:
            return LogicalType("Float"), True, False, False, True, None
        # AVG, MEDIAN, PERCENTILE_CONT: an exact mean or interpolation takes `rounding`.
        if logical.datatype in ("Integer", "Decimal"):
            typed, _ = _decimal_type(MAX_PRECISION, self.context.rounding)
            precision, scale = _decimal(logical)
            overflow = _exceeds(precision, scale, 0, self.context.rounding)
            return typed, False, False, overflow, True, semantic
        return logical, False, False, False, True, semantic


class _Scalars:
    """Scalar functions of the subset (spec §Functions)."""

    STRING_RESULT = frozenset(
        {
            "LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM", "LEFT", "RIGHT", "SUBSTRING", "REPLACE",
            "SPLIT_PART", "REGEXP_EXTRACT", "REGEXP_REPLACE", "CONCAT",
        }
    )  # fmt: skip
    INTEGER_RESULT = frozenset({"LENGTH", "CHARINDEX", "POSITION", "REGEXP_COUNT"})
    BOOLEAN_RESULT = frozenset(["CONTAINS", "STARTSWITH", "ENDSWITH", "REGEXP_LIKE"])
    DATE_PART_FUNCTIONS = frozenset(["YEAR", "QUARTER", "MONTH", "DAY", "DAYOFYEAR"])
    TIME_PART_FUNCTIONS = frozenset(["HOUR", "MINUTE", "SECOND"])
    ARITY: Mapping[str, tuple[int, int]] = MappingProxyType(
        {
            "LOWER": (1, 1),
            "UPPER": (1, 1),
            "TRIM": (1, 1),
            "LTRIM": (1, 1),
            "RTRIM": (1, 1),
            "LEFT": (2, 2),
            "RIGHT": (2, 2),
            "SUBSTRING": (2, 3),
            "REPLACE": (3, 3),
            "SPLIT_PART": (3, 3),
            "REGEXP_EXTRACT": (2, 3),
            "REGEXP_REPLACE": (3, 3),
            "CONCAT": (1, 255),
            "LENGTH": (1, 1),
            "CHARINDEX": (2, 2),
            "POSITION": (2, 2),
            "REGEXP_COUNT": (2, 2),
            "CONTAINS": (2, 2),
            "STARTSWITH": (2, 2),
            "ENDSWITH": (2, 2),
            "REGEXP_LIKE": (2, 2),
            "TO_DATE": (1, 1),
            "TO_TIMESTAMP": (1, 1),
            "ABS": (1, 1),
            "SIGN": (1, 1),
            "ROUND": (1, 2),
            "TRUNC": (1, 2),
            "TRUNCATE": (1, 2),
            "FLOOR": (1, 1),
            "CEIL": (1, 1),
            "CEILING": (1, 1),
            "MOD": (2, 2),
            "POWER": (2, 2),
            "ATAN2": (2, 2),
            "LOG": (1, 2),
            "PI": (0, 0),
            "GREATEST": (1, 255),
            "LEAST": (1, 255),
            "COALESCE": (1, 255),
            "IFNULL": (2, 2),
            "NVL": (2, 2),
            "NVL2": (3, 3),
            "IF": (3, 3),
            "IFF": (3, 3),
            "NULLIF": (2, 2),
            "ZEROIFNULL": (1, 1),
            "NULLIFZERO": (1, 1),
            "CURRENT_DATE": (0, 0),
            "CURRENT_TIME": (0, 0),
            "CURRENT_TIMESTAMP": (0, 0),
            **dict.fromkeys(
                (
                    "YEAR",
                    "QUARTER",
                    "MONTH",
                    "DAY",
                    "DAYOFYEAR",
                    "HOUR",
                    "MINUTE",
                    "SECOND",
                    "SQRT",
                    "EXP",
                    "LN",
                    "LOG10",
                    "SIN",
                    "COS",
                    "TAN",
                    "ASIN",
                    "ACOS",
                    "ATAN",
                    "RADIANS",
                    "DEGREES",
                ),
                (1, 1),
            ),
        }
    )
    STRING_ARGUMENTS: Mapping[str, tuple[int, ...]] = MappingProxyType(
        {
            "LEFT": (0,),
            "RIGHT": (0,),
            "SUBSTRING": (0,),
            "SPLIT_PART": (0, 1),
            "REGEXP_EXTRACT": (0, 1),
        }
    )

    @classmethod
    def type(cls, node: sql.Call, arguments: tuple[TypedNode, ...], checker: _Checker) -> TypedNode:
        name = node.name
        arity = cls.ARITY.get(name)
        if arity is None:
            raise _Bad()  # every scalar has an explicit signature; anything else is refused
        low, high = arity
        if not low <= len(arguments) <= high:
            raise _Bad()
        logical, extra = cls._result(name, arguments, checker)
        return TypedNode(node, _merge(arguments, logical, **extra), arguments)

    @classmethod
    def _result(
        cls, name: str, arguments: tuple[TypedNode, ...], checker: _Checker
    ) -> tuple[LogicalType, _Extra]:
        integer = LogicalType("Integer", width=64, signed=True)
        if name in cls.STRING_RESULT or name in cls.INTEGER_RESULT or name in cls.BOOLEAN_RESULT:
            strings = cls.STRING_ARGUMENTS.get(name, tuple(range(len(arguments))))
            for at, argument in enumerate(arguments):
                _need(argument, STRING if at in strings else frozenset({"Integer"}))
            if name in cls.STRING_RESULT:
                return LogicalType("String"), {}
            return (integer if name in cls.INTEGER_RESULT else LogicalType("Boolean")), {}
        if name in ("TO_DATE", "TO_TIMESTAMP"):
            _need(arguments[0], STRING)
            return LogicalType("Date" if name == "TO_DATE" else "DateTime"), {}
        if name in cls.DATE_PART_FUNCTIONS or name in cls.TIME_PART_FUNCTIONS:
            value = arguments[0]
            _need(value, DATED if name in cls.DATE_PART_FUNCTIONS else TIMED)
            instant = value.type.logical.datatype == "DateTimeTz"
            zone = checker._zone(value) if instant else None
            return integer, {"bucket_zone": zone, "buckets_instant": instant}
        if name in ("CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"):
            result = {"CURRENT_DATE": "Date", "CURRENT_TIME": "Time"}.get(name, "DateTimeTz")
            return LogicalType(result), {"volatile": True, "nullable": False}  # type: ignore[arg-type]
        if name in APPROXIMATING:
            for argument in arguments:
                _need(argument, NUMERIC)
                if argument.type.semantic_type in NO_ARITHMETIC:
                    raise _Bad()
            return LogicalType("Float"), {"approximate": True}
        return cls._numeric_or_choice(name, arguments)

    @classmethod
    def _numeric_or_choice(
        cls, name: str, arguments: tuple[TypedNode, ...]
    ) -> tuple[LogicalType, _Extra]:
        if name in ("GREATEST", "LEAST", "COALESCE", "IFNULL", "NVL"):
            logical, overflow = _unify(arguments)
            nullable = (
                all(a.type.nullable for a in arguments)
                if name in ("COALESCE", "IFNULL", "NVL")
                else any(a.type.nullable for a in arguments)
            )
            return logical, {
                "overflow": overflow,
                "nullable": nullable,
                "semantic": _semantic(arguments),
            }
        if name in ("IF", "IFF", "NVL2"):
            if name != "NVL2":
                _need(arguments[0], BOOLEAN)
            choices = arguments[1:]
            logical, overflow = _unify(choices)
            nullable = any(c.type.nullable for c in choices)
            return logical, {
                "overflow": overflow,
                "nullable": nullable,
                "semantic": _semantic(choices),
            }
        if name == "NULLIF":
            _comparable(*arguments)
            first = arguments[0].type
            return first.logical, {"nullable": True, "semantic": first.semantic_type}
        value = arguments[0]
        _need(value, NUMERIC)
        logical, semantic = value.type.logical, value.type.semantic_type
        if name in ("ZEROIFNULL", "NULLIFZERO"):
            if name == "ZEROIFNULL" and semantic in NO_ARITHMETIC:
                raise _Bad()
            return logical, {"nullable": name == "NULLIFZERO", "semantic": semantic}
        if semantic in NO_ARITHMETIC:
            raise _Bad()
        if name == "SIGN":
            return LogicalType("Integer", width=64, signed=True), {}
        if name == "ABS":
            overflow = logical.datatype == "Integer"
            return logical, {"semantic": semantic, "overflow": overflow}
        if name == "MOD":
            _need(arguments[1], NUMERIC)
            _comparable(value, arguments[1])
            if logical.datatype == "Float" or arguments[1].type.logical.datatype == "Float":
                return LogicalType("Float"), {"semantic": semantic}
            shapes = [_decimal(a.type.logical) for a in arguments]
            if "Decimal" not in (logical.datatype, arguments[1].type.logical.datatype):
                return _widest_integer([a.type.logical for a in arguments]), {"semantic": semantic}
            if any(p is None or s is None for p, s in shapes):
                return LogicalType("Decimal", MAX_PRECISION, None), {
                    "semantic": semantic,
                    "nullable": True,
                }
            scale = max(s for _, s in shapes if s is not None)
            whole = max(p - s for p, s in shapes if p is not None and s is not None)
            typed, overflow = _decimal_type(whole + scale, scale)
            return typed, {"semantic": semantic, "overflow": overflow, "nullable": True}
        if name in ("ROUND", "TRUNC", "TRUNCATE", "FLOOR", "CEIL", "CEILING"):
            return cls._rounding(name, arguments, logical, semantic)
        raise _Bad()

    @staticmethod
    def _rounding(
        name: str, arguments: tuple[TypedNode, ...], logical: LogicalType, semantic: str | None
    ) -> tuple[LogicalType, _Extra]:
        """ROUND, TRUNC, FLOOR, CEIL: the kept digits, plus one for a carry when rounding up."""
        digits = 0
        if len(arguments) == 2:
            places = arguments[1]
            if not isinstance(places.node, sql.Number) or not places.node.text.isdigit():
                raise _Bad()  # the number of places is a literal
            digits = int(places.node.text)
        if logical.datatype != "Decimal" or logical.precision is None or logical.scale is None:
            return logical, {"semantic": semantic}
        scale = min(logical.scale, digits)
        carry = 0 if name in ("TRUNC", "TRUNCATE") else 1
        typed, overflow = _decimal_type(logical.precision - logical.scale + scale + carry, scale)
        return typed, {"semantic": semantic, "overflow": overflow}


# --- units and currencies (P2-04d) --------------------------------------------------------------

UNDECLARED = "undeclared"
_SAME_UNIT_FUNCTIONS = frozenset(
    {"MOD", "GREATEST", "LEAST", "COALESCE", "IFNULL", "NVL", "NULLIF"}
)
_KEEP_UNIT_FUNCTIONS = frozenset(
    {"ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "TRUNC", "TRUNCATE", "ZEROIFNULL", "NULLIFZERO"}
)
_DIMENSIONLESS_FUNCTIONS = frozenset(
    """
    EXP LN LOG LOG10 SIN COS TAN ASIN ACOS ATAN ATAN2 RADIANS DEGREES
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)
_COUNTS = frozenset({"COUNT", "APPROX_COUNT_DISTINCT"})
_SQUARING = frozenset({"VARIANCE", "VAR_POP", "VAR_SAMP"})


class _Mismatch(Exception):
    """Two units that cannot meet: `INCOMPATIBLE_UNITS`."""

    def __init__(self, left: Unit | None, right: Unit | None) -> None:
        super().__init__("INCOMPATIBLE_UNITS")
        self.units = [_shown(left), _shown(right)]


def _shown(unit: Unit | None) -> str:
    return unit.text if unit is not None else UNDECLARED


def _same(children: Sequence[TypedNode]) -> Unit | None:
    """`+`, `-`, comparisons and alternatives: every operand but a bare literal has the same
    unit, dimension and factor, and an undeclared unit never meets a declared one."""
    kept = [child.type.unit for child in children if not child.type.constant]
    if not kept:
        return None
    for other in kept[1:]:
        if other != kept[0]:
            raise _Mismatch(kept[0], other)
    return kept[0]


def _product(left: TypedNode, right: TypedNode, divide: bool) -> Unit | None:
    """`*` and `/` combine units; a bare literal is dimensionless; undeclared stays undeclared."""
    a = ONE if left.type.constant else left.type.unit
    b = ONE if right.type.constant else right.type.unit
    if left.type.constant and right.type.constant:
        return None
    if a is None or b is None:
        return None
    return a / b if divide else a * b


def _dimensionless(children: Sequence[TypedNode]) -> Unit | None:
    units = [child.type.unit for child in children if not child.type.constant]
    for unit in units:
        if unit is not None and unit.dimensions:
            raise _Mismatch(unit, ONE)
    return ONE if units and all(unit is not None for unit in units) else None


def _unit_of(node: sql.Node, own: NodeType, children: tuple[TypedNode, ...]) -> Unit | None:
    if isinstance(node, sql.Ref):
        return own.unit
    if isinstance(node, sql.Unary):
        return children[0].type.unit
    if isinstance(node, sql.Binary):
        if node.op in ("+", "-", "%"):
            return _same(children)
        if node.op in ("*", "/"):
            return _product(children[0], children[1], node.op == "/")
        if node.op in ("=", "<>", "<", ">", "<=", ">="):
            _same(children)
        return None
    if isinstance(node, sql.Between | sql.InList | sql.IsDistinct):
        _same(children)
        return None
    if isinstance(node, sql.Case):
        results = len(node.whens) + (1 if node.otherwise is not None else 0)
        if node.operand is not None:
            _same(children[: len(children) - results])
        return _same(children[len(children) - results :])
    if isinstance(node, sql.Cast):
        return children[0].type.unit if own.logical.datatype in NUMERIC else None
    if isinstance(node, sql.Call):
        return _call_unit(node, children)
    return None


def _call_unit(node: sql.Call, children: tuple[TypedNode, ...]) -> Unit | None:
    name = node.name
    if name in sql.AGGREGATES:
        values = children[:-1] if node.filter is not None else children
        if name in _COUNTS:
            return ONE
        unit = values[0].type.unit if values else None
        if name in _SQUARING:
            return unit.power(2) if unit is not None else None
        return unit
    if name in _KEEP_UNIT_FUNCTIONS:
        return children[0].type.unit
    if name in _SAME_UNIT_FUNCTIONS:
        return _same(children)
    if name in ("IF", "IFF", "NVL2"):
        return _same(children[1:])
    if name == "SIGN":
        return ONE if children[0].type.unit is not None else None
    if name == "SQRT":
        unit = children[0].type.unit
        if unit is None:
            return None
        root = unit.root()
        if root is None:
            raise _Mismatch(unit, ONE)
        return root
    if name == "POWER":
        base, exponent = children
        if isinstance(exponent.node, sql.Number) and exponent.node.text.isdigit():
            return base.type.unit.power(int(exponent.node.text)) if base.type.unit else None
        return _dimensionless(children)
    if name in _DIMENSIONLESS_FUNCTIONS:
        return _dimensionless(children)
    return None


def _units(tree: TypedNode) -> TypedNode:
    """The tree with each node's unit, bottom up (#280 §2 units and aggregates)."""
    children = tuple(_units(child) for child in tree.children)
    unit = _unit_of(tree.node, tree.type, children)
    return TypedNode(tree.node, replace(tree.type, unit=unit), children)


# --- the model ---------------------------------------------------------------------------------


def _payloads(model: OssieModel) -> dict[str, tuple[str, Json]]:
    found: dict[str, tuple[str, Json]] = {}
    for extension in model.product_extensions:
        element = extension.pointer.rsplit("/custom_extensions/", 1)[0]
        found[element] = (f"{extension.pointer}/data", extension.payload)
    return found


def _node_at(document: Json, pointer: str) -> Json:
    current: object = document
    for part in pointer.strip("/").split("/"):
        current = current[int(part)] if isinstance(current, tuple) else current[part]  # type: ignore[index]
    assert isinstance(current, Mapping)
    return current


class _Model:
    def __init__(self, model: OssieModel, parsed: ParsedModel, catalog: SourceCatalog) -> None:
        self.model = model
        self.parsed = parsed
        self.catalog = {
            source: {column_key(name): column for name, column in columns.items()}
            for source, columns in catalog.items()
        }
        self.payloads = _payloads(model)
        self.texts = {e.element: e.pointer for e in model.expressions}
        self.problems = Problems(model)
        self.typed: dict[MemberId, TypedMember] = {}
        self.failed: set[MemberId] = set()
        self.mismatches: dict[MemberId, list[str]] = {}
        self.expression_mismatches: list[list[str]] = []
        self.sources = {
            sql.normalize(str(d["name"])): str(d.get("source", d["name"]))
            for d in _items(model.document, "datasets")
        }
        _, model_payload = self.payloads.get("", ("", MappingProxyType({})))
        self.default_zone = model_payload.get("default_time_zone")
        if self.default_zone is not None and not is_zone(str(self.default_zone)):
            self.problems.add(self.payloads[""][0], "extension")
            self.default_zone = None

    # members

    def member(self, member: MemberId) -> NodeType:
        if member in self.failed or member not in self.parsed.members:
            raise _Skip
        if member not in self.typed:
            self._type(self.parsed.members[member])
        if member in self.failed:
            raise _Skip
        return self.typed[member].type

    def _type(self, member: ParsedMember) -> None:
        data_at, payload = self.payloads.get(member.pointer, ("", MappingProxyType({})))
        text_at = self.texts.get(member.pointer, member.pointer)
        at = text_at
        try:
            zone = self._declared_zone(payload, data_at)
            if member.id.kind == "field":
                typed = self._field(member, payload, zone)
            else:
                typed = self._metric(member, payload)
            at = data_at
            extra = tuple(
                (label, _units(tree)) for label, tree in self._payload_trees(member, payload, zone)
            )
            at = text_at
            typed = self._declarations(member, typed, payload, data_at, zone, extra)
        except _Bad as bad:
            self.failed.add(member.id)
            self.problems.add(bad.pointer or at, bad.rule)
            return
        except _Skip:
            self.failed.add(member.id)
            return
        except _Mismatch as mismatch:
            self.failed.add(member.id)
            self.mismatches[member.id] = mismatch.units
            return
        tree, root = typed
        self.typed[member.id] = TypedMember(member.id, root, tree, extra)

    def _declared_zone(self, payload: Json, data_at: str) -> str | None:
        declared = payload.get("time_zone")
        if not isinstance(declared, Mapping):
            return None
        name = str(declared["name"])
        if not is_zone(name):
            raise _Bad("extension", data_at)
        return name

    def _field(
        self, member: ParsedMember, payload: Json, zone: str | None
    ) -> tuple[TypedNode | None, NodeType]:
        if member.tree is None:
            return None, self._opaque(member)
        source = self.catalog.get(self.sources[member.id.dataset or ""], {})
        own = member.id.dataset

        def column(ref: sql.Ref) -> NodeType:
            parts = ref.parts
            if len(parts) == 2 and parts[0] == own:
                parts = parts[1:]
            found: SourceColumn | None = (
                source.get(column_key(parts[0])) if len(parts) == 1 else None
            )
            if found is None:
                raise _Bad("reference")
            logical = logical_of(found.type)
            return _merge(
                (),
                logical,
                nullable=found.nullable,
                approximate=logical.datatype == "Float",
                constant=False,
            )

        context = _Context(column, None, zone, lambda ref: own, self._zone_or_none())
        tree = _Checker(context).check(member.tree)
        if tree.type.kind != "row" or tree.type.aggregate_class is not None:
            raise _Bad()  # a field is row-level
        return tree, replace(tree.type, constant=False)

    def _zone_or_none(self) -> str | None:
        return str(self.default_zone) if self.default_zone is not None else None

    def _metric(self, member: ParsedMember, payload: Json) -> tuple[TypedNode | None, NodeType]:
        if member.tree is None:
            return None, self._opaque(member)
        tree = _Checker(self._context(member, payload, own_row=None)).check(member.tree)
        if tree.type.kind == "row":
            raise _Bad()  # a metric is an aggregate
        return tree, replace(tree.type, kind="metric", constant=False)

    def _context(
        self,
        member: ParsedMember,
        payload: Json,
        own_row: str | None,
        own_zone: str | None = None,
    ) -> _Context:
        rounding = payload.get("rounding")
        scale = int(str(rounding["scale"])) if isinstance(rounding, Mapping) else None
        owner = member.id
        language = "product" if own_row is not None else member.language

        def target(ref: sql.Ref) -> MemberId:
            parts = ref.parts
            if language == "ossie":
                if owner.dataset is not None and len(parts) == 1:
                    return MemberId("field", owner.dataset, parts[0])
                if owner.dataset is None and len(parts) == 2:
                    return MemberId("field", parts[0], parts[1])
                raise _Skip  # refused by P2-04b already
            if len(parts) == 1:
                if own_row is not None:
                    return MemberId("field", own_row, parts[0])
                return MemberId("metric", None, parts[0])
            field = MemberId("field", parts[0], parts[1])
            return field if field in self.parsed.members else MemberId("metric", parts[0], parts[1])

        def resolve(ref: sql.Ref) -> NodeType:
            found = target(ref)
            typed = self.member(found)
            if found.kind == "metric":
                return replace(typed, kind="metric", constant=False)
            return replace(typed, kind="row", constant=False)

        def dataset_of(ref: sql.Ref) -> str | None:
            return target(ref).dataset

        return _Context(resolve, scale, own_zone, dataset_of, self._zone_or_none())

    def _opaque(self, member: ParsedMember) -> NodeType:
        """Typed from its declarations; a metric's aggregate class is holistic (#280 §2)."""
        node = _node_at(self.model.document, member.pointer)
        datatype = node.get("datatype")
        logical = LogicalType(str(datatype) if isinstance(datatype, str) else None)  # type: ignore[arg-type]
        metric = member.id.kind == "metric"
        return NodeType(
            logical=logical,
            semantic_type=None,
            nullable=True,
            exactness="approximate"
            if logical.datatype == "Float"
            else "unknown"
            if logical.datatype is None
            else "exact",
            kind="metric" if metric else "row",
            aggregate_class="holistic" if metric else None,
            volatility="query_instant",  # never interpreted: assumed to vary
            overflow_check=False,
            constant=False,
        )

    def _payload_trees(
        self, member: ParsedMember, payload: Json, zone: str | None
    ) -> tuple[tuple[str, TypedNode], ...]:
        found: list[tuple[str, TypedNode]] = []
        calculated = payload.get("calculated")
        if isinstance(calculated, Mapping):
            # The field's own `time_zone` buckets its calculated expression too.
            context = self._context(member, payload, member.id.dataset, zone)
            tree = _Checker(context).check(sql.parse(str(calculated["expression"])))
            if tree.type.kind != "row" or tree.type.aggregate_class is not None:
                raise _Bad()
            found.append(("calculated", tree))
        over = payload.get("over_metrics")
        if isinstance(over, Mapping):
            tree = _Checker(self._context(member, payload, None)).check(
                sql.parse(str(over["expression"]))
            )
            if tree.type.kind == "row":
                raise _Bad()
            found.append(("over_metrics", tree))
        components = payload.get("components")
        if isinstance(components, Mapping):
            for part in ("numerator", "denominator"):
                tree = _Checker(self._context(member, payload, None)).check(
                    sql.parse(str(components[part]))
                )
                _need(tree, NUMERIC)
                if tree.type.kind == "row":
                    raise _Bad()
                found.append((f"components.{part}", tree))
        return tuple(found)

    def _declarations(
        self,
        member: ParsedMember,
        typed: tuple[TypedNode | None, NodeType],
        payload: Json,
        data_at: str,
        zone: str | None,
        extra: tuple[tuple[str, TypedNode], ...],
    ) -> tuple[TypedNode | None, NodeType]:
        """A declared `datatype` and `numeric` equal the inferred type; the declared zone,
        nullability and semantic type are the member's own (#280 §2). So are the declared unit
        or currency, which must equal any unit the expression itself computes."""
        tree, root = typed
        if tree is not None:
            tree = _units(tree)
            root = replace(root, unit=tree.type.unit)
        root = replace(root, unit=self._declared_unit(root.unit, payload, data_at, extra))
        node = _node_at(self.model.document, member.pointer)
        logical = root.logical
        declared = node.get("datatype")
        if (
            isinstance(declared, str)
            and logical.datatype is not None
            and declared != logical.datatype
        ):
            raise _Bad("type", f"{member.pointer}/datatype")
        numeric = payload.get("numeric")
        if isinstance(numeric, Mapping):
            logical = self._numeric(logical, numeric, data_at)
        if zone is not None and logical.datatype in ("DateTime", "DateTimeTz"):
            logical = replace(logical, zone=zone)
        nullability = payload.get("nullability")
        nullable = root.nullable if nullability is None else nullability == "nullable"
        semantic = payload.get("semantic_type")
        root = replace(
            root,
            logical=logical,
            nullable=nullable,
            semantic_type=str(semantic) if semantic is not None else root.semantic_type,
        )
        return tree, root

    @staticmethod
    def _declared_unit(
        inferred: Unit | None, payload: Json, data_at: str, extra: tuple[tuple[str, TypedNode], ...]
    ) -> Unit | None:
        """The declared `unit` (UCUM, the registry) or `currency` (the ISO 4217 snapshot): an
        unknown one is `extension`; one that differs from what the expression or its
        `calculated` and `over_metrics` expressions compute is `INCOMPATIBLE_UNITS`."""
        unit, code = payload.get("unit"), payload.get("currency")
        if unit is not None and code is not None:
            raise _Bad("extension", data_at)  # one measure, one unit
        try:
            if unit is not None:
                declared = parse_unit(str(unit))
            elif code is not None:
                declared = currency_unit(str(code))
            else:
                declared = None
        except UnitError:
            raise _Bad("extension", data_at) from None
        computed = [inferred] + [
            tree.type.unit for label, tree in extra if not label.startswith("components")
        ]
        for found in computed:
            if declared is not None and found is not None and found != declared:
                raise _Mismatch(declared, found)
        return declared if declared is not None else inferred

    @staticmethod
    def _numeric(logical: LogicalType, numeric: Json, data_at: str) -> LogicalType:
        def number(key: str) -> int | None:
            value = numeric.get(key)
            return int(str(value)) if value is not None else None

        if logical.datatype == "Decimal":
            pairs = (("precision", logical.precision), ("scale", logical.scale))
            for key, inferred in pairs:
                declared = number(key)
                if declared is not None and inferred is not None and declared != inferred:
                    raise _Bad("type", data_at)
            return replace(
                logical,
                precision=logical.precision
                if logical.precision is not None
                else number("precision"),
                scale=logical.scale if logical.scale is not None else number("scale"),
            )
        if logical.datatype == "Integer":
            width, signed = number("width"), numeric.get("signed")
            if (width is not None and logical.width is not None and width != logical.width) or (
                signed is not None and logical.signed is not None and signed != logical.signed
            ):
                raise _Bad("type", data_at)
            return replace(
                logical,
                width=logical.width if logical.width is not None else width,
                signed=logical.signed if logical.signed is not None else signed,  # type: ignore[arg-type]
            )
        return logical

    # model payload expressions

    def model_expressions(self) -> tuple[tuple[str, TypedNode], ...]:
        data_at, payload = self.payloads.get("", ("", MappingProxyType({})))
        found: list[tuple[str, TypedNode]] = []
        texts: list[tuple[str, object, bool]] = []  # (label, text, boolean)
        for group in _items(payload, "calculation_groups"):
            for item in _items(group, "items"):
                texts.append(
                    (
                        f"calculation_groups.{group['name']}.{item['name']}",
                        item["expression"],
                        False,
                    )
                )
        for segment in _items(payload, "segments"):
            texts.append((f"segments.{segment['name']}", segment["condition"], True))
        for funnel in _items(payload, "funnels"):
            steps = funnel.get("steps")
            for number, step in enumerate(steps if isinstance(steps, tuple) else ()):
                texts.append((f"funnels.{funnel['name']}.{number}", step, True))
        empty = ParsedMember(MemberId("metric", None, ""), "", "", "product", None, (), ())
        context = self._context(empty, MappingProxyType({}), None)
        for label, text, boolean in texts:
            try:
                tree = _units(_Checker(context).check(sql.parse(str(text))))
                if boolean:
                    _need(tree, BOOLEAN)
            except _Mismatch as mismatch:
                self.expression_mismatches.append(mismatch.units)
                continue
            except _Bad as bad:
                self.problems.add(bad.pointer or data_at, bad.rule)
                continue
            except _Skip, sql.SqlSyntaxError, sql.SqlUnsupported:
                continue  # refused or excluded by P2-04b
            found.append((label, tree))
        return tuple(found)


def _items(node: Json, key: str) -> list[Json]:
    value = node.get(key)
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, tuple) else []


def type_model(model: OssieModel, parsed: ParsedModel, catalog: SourceCatalog) -> TypedModel:
    """Type every compiled member and payload expression, or raise `SemanticError`."""
    state = _Model(model, parsed, catalog)
    for member in sorted(parsed.members):
        try:
            state.member(member)
        except _Skip:
            continue
    expressions = state.model_expressions()
    state.problems.raise_if_any()
    # The first mismatch in a fixed order: members, then model expressions as written.
    found = [state.mismatches[m] for m in sorted(state.mismatches)] + state.expression_mismatches
    if found:
        raise SemanticError("INCOMPATIBLE_UNITS", {"units": found[0]})
    return TypedModel(parsed, MappingProxyType(dict(state.typed)), expressions, TZDATA_VERSION)
