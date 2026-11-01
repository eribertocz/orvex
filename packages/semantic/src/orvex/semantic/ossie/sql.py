"""A parser for the OSSIE_SQL_2026 expression subset (`expression_language.md` at `c721ffc`).

Conformance and product capability are kept apart (ADR-0030 §2):
- `parse` turns one expression of the subset's grammar into an immutable tree, window functions
  and EXPERIMENTAL functions included, so references are checked on every valid expression. A
  text outside the grammar raises `SqlSyntaxError`: a breach of the specification, as in the
  official validator (`OSSIE_INVALID`, rule `expression`).
- Recognizable SQL outside the subset (a subquery, `::`, `INTERVAL`, a typed literal, an
  operator or sigil of some dialect such as `^`, `[`, `#`, `$`) raises `SqlUnsupported`, as no
  tree can be built for it. When in doubt, a text is unsupported rather than invalid: only a
  text no SQL dialect could read as one expression of the subset (empty, unbalanced, a
  structural keyword where an operand belongs, text after the expression) is a syntax error.
- `unsupported(tree)` names the first construct of a valid tree that the product does not
  implement (a window `OVER`, an EXPERIMENTAL or unknown function): `UNSUPPORTED_OPERATION`,
  with a fixed `operation` name, never text from the document.

Identifiers follow L86-L88: unquoted ones are case-insensitive (normalized to upper case),
double-quoted ones exact; at most 128 characters. A word that names a date part or a type
(`date`, `month`, `time`…) reads as an identifier where an operand is expected, as the official
validator's SQL parser reads it (`SUM(date)` reads the field `DATE`); a structural keyword
(`AND`, `SELECT`, `WHEN`…) never does. Numbers keep their
written text, so no exact value becomes a float. The same grammar reads the product's
expression language, whose references may also name metrics (P2-01).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MAX_IDENTIFIER = 128
MAX_DEPTH = 64

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<string>'(?:[^']|'')*')
  | (?P<quoted>"(?:[^"]|"")+")
  | (?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)
  | (?P<word>[^\W\d][\w$]*)
  | (?P<symbol><>|<=|>=|!=|\|\||::|[-+*/%(),.<>=])
  | (?P<foreign>[\^\[\]{}#$?&~!@])
    """,
    re.VERBOSE | re.DOTALL,
)

# SQL reserved words this subset uses or forbids; never an unquoted identifier.
RESERVED = frozenset(
    """
    ALL AND ANY AS BETWEEN BY CASE CAST CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP DATE DAY
    DISTINCT ELSE END ESCAPE EXCEPT EXISTS EXTRACT FALSE FILTER FROM GROUP HOUR ILIKE IN
    INTERSECT INTERVAL IS JOIN LIKE MINUTE MONTH NOT NULL ON OR ORDER OVER POSITION SECOND
    SELECT SOME THEN TIME TIMESTAMP TRUE TRY_CAST UNION WHEN WHERE WITH WITHIN YEAR
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)
# Reserved words that still read as identifiers in operand position.
SOFT_RESERVED = frozenset(
    {"DATE", "DAY", "HOUR", "MINUTE", "MONTH", "POSITION", "SECOND", "TIME", "TIMESTAMP", "YEAR"}
)
DATE_PARTS = frozenset(
    """
    YEAR QUARTER MONTH WEEK DAY DAYOFWEEK DAYOFYEAR HOUR MINUTE SECOND MILLISECOND
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)
CAST_TYPES = frozenset(
    """
    VARCHAR STRING INTEGER INT BIGINT DECIMAL NUMERIC FLOAT DOUBLE BOOLEAN DATE TIMESTAMP TIME
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)

AggregateClass = Literal["distributive", "algebraic", "sketch", "holistic"]
AGGREGATE_CLASSES: tuple[AggregateClass, ...] = ("distributive", "algebraic", "sketch", "holistic")
AGGREGATES: dict[str, AggregateClass] = {
    "SUM": "distributive",
    "COUNT": "distributive",
    "MIN": "distributive",
    "MAX": "distributive",
    "AVG": "algebraic",
    "STDDEV": "algebraic",
    "STDDEV_POP": "algebraic",
    "STDDEV_SAMP": "algebraic",
    "VARIANCE": "algebraic",
    "VAR_POP": "algebraic",
    "VAR_SAMP": "algebraic",
    "MEDIAN": "holistic",
    "PERCENTILE_CONT": "holistic",
    "PERCENTILE_DISC": "holistic",
    "APPROX_COUNT_DISTINCT": "sketch",
    "APPROX_PERCENTILE": "sketch",
}
# Scalar functions the subset defines as REQUIRED or RECOMMENDED.
SCALARS = frozenset(
    """
    YEAR QUARTER MONTH DAY DAYOFYEAR HOUR MINUTE SECOND DATE_PART DATE_TRUNC DATEADD DATEDIFF
    TO_DATE TO_TIMESTAMP CONCAT LENGTH LOWER UPPER TRIM LTRIM RTRIM LEFT RIGHT SUBSTRING REPLACE
    SPLIT_PART CHARINDEX CONTAINS STARTSWITH ENDSWITH REGEXP_LIKE REGEXP_EXTRACT REGEXP_REPLACE
    REGEXP_COUNT ABS ROUND FLOOR CEIL CEILING TRUNC TRUNCATE MOD SIGN POWER SQRT EXP LN LOG LOG10
    SIN COS TAN ASIN ACOS ATAN ATAN2 RADIANS DEGREES PI GREATEST LEAST IF IFF NULLIF COALESCE
    IFNULL NVL NVL2 ZEROIFNULL NULLIFZERO
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)
WINDOW_ONLY = frozenset(
    """
    ROW_NUMBER RANK DENSE_RANK NTILE PERCENT_RANK CUME_DIST LAG LEAD FIRST_VALUE LAST_VALUE
    NTH_VALUE
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)
# Functions with a date-part keyword as their first argument.
_PART_FIRST = frozenset({"DATEADD", "DATEDIFF", "DATE_TRUNC", "DATE_PART"})


class SqlSyntaxError(ValueError):
    """Not an expression of the subset's grammar."""


class SqlUnsupported(ValueError):
    """Valid SQL the product does not implement; `operation` is a fixed name."""

    def __init__(self, operation: str) -> None:
        super().__init__(operation)
        self.operation = operation


# --- the tree ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Number:
    text: str  # as written: exact


@dataclass(frozen=True, slots=True)
class String:
    value: str


@dataclass(frozen=True, slots=True)
class Boolean:
    value: bool


@dataclass(frozen=True, slots=True)
class Null:
    pass


@dataclass(frozen=True, slots=True)
class Ref:
    """A reference of one or two parts, each normalized (L86)."""

    parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Star:
    pass


@dataclass(frozen=True, slots=True)
class DatePart:
    part: str


@dataclass(frozen=True, slots=True)
class Unary:
    op: str
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class Between:
    operand: Node
    low: Node
    high: Node
    negated: bool


@dataclass(frozen=True, slots=True)
class InList:
    operand: Node
    items: tuple[Node, ...]
    negated: bool


@dataclass(frozen=True, slots=True)
class Like:
    operand: Node
    pattern: Node
    escape: Node | None
    insensitive: bool
    negated: bool


@dataclass(frozen=True, slots=True)
class IsNull:
    operand: Node
    negated: bool


@dataclass(frozen=True, slots=True)
class IsDistinct:
    left: Node
    right: Node
    negated: bool


@dataclass(frozen=True, slots=True)
class Case:
    operand: Node | None
    whens: tuple[tuple[Node, Node], ...]
    otherwise: Node | None


@dataclass(frozen=True, slots=True)
class Cast:
    operand: Node
    type_name: str
    arguments: tuple[int, ...]
    safe: bool  # TRY_CAST


@dataclass(frozen=True, slots=True)
class Window:
    """A function with an `OVER` clause; the frame is kept as its words."""

    call: Call
    partition: tuple[Node, ...]
    order: tuple[tuple[Node, bool], ...]  # (expression, descending)
    frame: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    arguments: tuple[Node, ...]
    distinct: bool = False
    filter: Node | None = None  # FILTER (WHERE ...)
    order: Node | None = None  # WITHIN GROUP (ORDER BY ...)
    descending: bool = False


Node = (
    Number
    | String
    | Boolean
    | Null
    | Ref
    | Star
    | DatePart
    | Unary
    | Binary
    | Not
    | Between
    | InList
    | Like
    | IsNull
    | IsDistinct
    | Case
    | Cast
    | Call
    | Window
)


def normalize(name: str) -> str:
    """The identity of an identifier: exact inside double quotes, upper case otherwise."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.upper()


# --- the parser -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    at = 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None:
            raise SqlSyntaxError("an unexpected character")
        at = match.end()
        kind = match.lastgroup
        assert kind is not None
        if kind == "foreign":
            # An operator or sigil of some SQL dialect, outside the subset: valid SQL the
            # product does not implement, never a breach of the specification (ADR-0030 §2).
            raise SqlUnsupported("operator")
        if kind not in ("space", "comment"):
            tokens.append(_Token(kind, match.group()))
    return tokens


_COMPARISONS = frozenset({"=", "<>", "!=", "<", ">", "<=", ">="})


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.at = 0
        self.depth = 0

    # helpers

    def peek(self, ahead: int = 0) -> _Token | None:
        index = self.at + ahead
        return self.tokens[index] if index < len(self.tokens) else None

    def word(self, *words: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token is not None and token.upper in words

    def symbol(self, *symbols: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token is not None and token.kind == "symbol" and token.text in symbols

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise SqlSyntaxError("the expression ends too early")
        self.at += 1
        return token

    def expect_symbol(self, symbol: str) -> None:
        if not self.symbol(symbol):
            raise SqlSyntaxError(f"'{symbol}' expected")
        self.at += 1

    def expect_word(self, word: str) -> None:
        if not self.word(word):
            raise SqlSyntaxError(f"{word} expected")
        self.at += 1

    def nested(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise SqlSyntaxError(f"nested more than {MAX_DEPTH} levels deep")

    # grammar, lowest precedence first

    def expression(self) -> Node:
        self.nested()
        try:
            return self.disjunction()
        finally:
            self.depth -= 1

    def disjunction(self) -> Node:
        node = self.conjunction()
        while self.word("OR"):
            self.at += 1
            node = Binary("OR", node, self.conjunction())
        return node

    def conjunction(self) -> Node:
        node = self.negation()
        while self.word("AND"):
            self.at += 1
            node = Binary("AND", node, self.negation())
        return node

    def negation(self) -> Node:
        if self.word("NOT"):
            self.at += 1
            self.nested()
            try:
                return Not(self.negation())
            finally:
                self.depth -= 1
        return self.comparison()

    def comparison(self) -> Node:
        node = self.additive()
        if self.symbol(*_COMPARISONS):
            op = self.take().text
            return Binary("<>" if op == "!=" else op, node, self.additive())
        negated = False
        if self.word("NOT") and self.word("BETWEEN", "IN", "LIKE", "ILIKE", ahead=1):
            self.at += 1
            negated = True
        if self.word("BETWEEN"):
            self.at += 1
            low = self.additive()
            self.expect_word("AND")
            return Between(node, low, self.additive(), negated)
        if self.word("IN"):
            self.at += 1
            self.expect_symbol("(")
            if self.word("SELECT"):
                raise SqlUnsupported("subquery")
            items = [self.expression()]
            while self.symbol(","):
                self.at += 1
                items.append(self.expression())
            self.expect_symbol(")")
            return InList(node, tuple(items), negated)
        if self.word("LIKE", "ILIKE"):
            insensitive = self.take().upper == "ILIKE"
            pattern = self.additive()
            escape = None
            if self.word("ESCAPE"):
                self.at += 1
                escape = self.additive()
            return Like(node, pattern, escape, insensitive, negated)
        if negated:
            raise SqlSyntaxError("NOT expects BETWEEN, IN, LIKE or ILIKE")
        if self.word("IS"):
            self.at += 1
            is_not = False
            if self.word("NOT"):
                self.at += 1
                is_not = True
            if self.word("NULL"):
                self.at += 1
                return IsNull(node, is_not)
            if self.word("DISTINCT"):
                self.at += 1
                self.expect_word("FROM")
                return IsDistinct(node, self.additive(), is_not)
            raise SqlSyntaxError("IS expects NULL or DISTINCT FROM")
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.symbol("+", "-", "||"):
            op = self.take().text
            node = Binary(op, node, self.multiplicative())
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.symbol("*", "/", "%"):
            op = self.take().text
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.symbol("+", "-"):
            op = self.take().text
            self.nested()
            try:
                return Unary(op, self.unary())
            finally:
                self.depth -= 1
        node = self.primary()
        if self.symbol("::"):
            raise SqlUnsupported("cast_operator")
        return node

    def primary(self) -> Node:
        token = self.take()
        if token.kind == "number":
            return Number(token.text)
        if token.kind == "string":
            return String(token.text[1:-1].replace("''", "'"))
        if token.kind == "symbol":
            if token.text == "(":
                if self.word("SELECT"):
                    raise SqlUnsupported("subquery")
                node = self.expression()
                self.expect_symbol(")")
                return node
            raise SqlSyntaxError("an operand expected")
        if token.kind == "quoted":
            return self.reference(token)
        word = token.upper
        if word in ("TRUE", "FALSE"):
            return Boolean(word == "TRUE")
        if word == "NULL":
            return Null()
        if word == "CASE":
            return self.case()
        if word in ("CAST", "TRY_CAST"):
            return self.cast(word == "TRY_CAST")
        if word == "EXTRACT":
            return self.extract()
        if word == "POSITION" and self.symbol("("):
            self.at += 1
            needle = self.additive()
            self.expect_word("IN")
            haystack = self.additive()
            self.expect_symbol(")")
            return Call("POSITION", (needle, haystack))
        if word in ("CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME"):
            if self.symbol("(") and self.symbol(")", ahead=1):
                self.at += 2
            return Call(word, ())
        if word == "SELECT" or word == "EXISTS":
            raise SqlUnsupported("subquery")
        if word == "INTERVAL":
            raise SqlUnsupported("interval")
        if word in ("DATE", "TIMESTAMP", "TIME") and self.peek() is not None:
            nxt = self.peek()
            if nxt is not None and nxt.kind == "string":
                raise SqlUnsupported("typed_literal")
        if self.symbol("("):
            return self.call(word)
        if word in RESERVED and word not in SOFT_RESERVED:
            raise SqlSyntaxError("a reserved word used as an identifier")
        return self.reference(token)

    def reference(self, first: _Token) -> Ref:
        parts = [first]
        while self.symbol("."):
            self.at += 1
            part = self.take()
            if part.kind not in ("word", "quoted"):
                raise SqlSyntaxError("a name expected after '.'")
            parts.append(part)
        if len(parts) > 2:
            raise SqlSyntaxError("a reference has one or two parts")
        names = tuple(normalize(part.text) for part in parts)
        if any(len(name) > MAX_IDENTIFIER for name in names):
            raise SqlSyntaxError("an identifier longer than 128 characters")
        return Ref(names)

    def case(self) -> Case:
        operand = None if self.word("WHEN") else self.expression()
        whens: list[tuple[Node, Node]] = []
        while self.word("WHEN"):
            self.at += 1
            condition = self.expression()
            self.expect_word("THEN")
            whens.append((condition, self.expression()))
        if not whens:
            raise SqlSyntaxError("CASE needs WHEN")
        otherwise = None
        if self.word("ELSE"):
            self.at += 1
            otherwise = self.expression()
        self.expect_word("END")
        return Case(operand, tuple(whens), otherwise)

    def cast(self, safe: bool) -> Cast:
        self.expect_symbol("(")
        operand = self.expression()
        self.expect_word("AS")
        token = self.take()
        if token.kind != "word":
            raise SqlSyntaxError("a type name expected")
        name = token.upper
        arguments: list[int] = []
        if self.symbol("("):
            self.at += 1
            while True:
                number = self.take()
                if number.kind != "number" or not number.text.isdigit():
                    raise SqlSyntaxError("a type argument is an integer")
                arguments.append(int(number.text))
                if self.symbol(")"):
                    self.at += 1
                    break
                self.expect_symbol(",")
        self.expect_symbol(")")
        if name not in CAST_TYPES:
            raise SqlUnsupported("cast_type")
        return Cast(operand, name, tuple(arguments), safe)

    def extract(self) -> Call:
        self.expect_symbol("(")
        token = self.take()
        if token.upper not in DATE_PARTS:
            raise SqlSyntaxError("EXTRACT expects a date part")
        self.expect_word("FROM")
        operand = self.expression()
        self.expect_symbol(")")
        return Call("EXTRACT", (DatePart(token.upper), operand))

    def call(self, name: str) -> Call | Window:
        self.expect_symbol("(")
        distinct = False
        arguments: list[Node] = []
        if self.word("DISTINCT", "ALL"):
            distinct = self.take().upper == "DISTINCT"
        if self.symbol(")"):
            self.at += 1
        elif self.symbol("*") and self.symbol(")", ahead=1):
            self.at += 2
            arguments.append(Star())
        else:
            while True:
                if not arguments and name in _PART_FIRST and self.peek() is not None:
                    first = self.peek()
                    assert first is not None
                    if (
                        first.kind == "word"
                        and first.upper in DATE_PARTS
                        and self.symbol(",", ahead=1)
                    ):
                        self.at += 1
                        arguments.append(DatePart(first.upper))
                        self.expect_symbol(",")
                        continue
                arguments.append(self.expression())
                if self.symbol(")"):
                    self.at += 1
                    break
                self.expect_symbol(",")
        order: Node | None = None
        descending = False
        if self.word("WITHIN"):
            self.at += 1
            self.expect_word("GROUP")
            self.expect_symbol("(")
            self.expect_word("ORDER")
            self.expect_word("BY")
            order = self.expression()
            if self.word("ASC", "DESC"):
                descending = self.take().upper == "DESC"
            self.expect_symbol(")")
        filtered: Node | None = None
        if self.word("FILTER"):
            self.at += 1
            self.expect_symbol("(")
            self.expect_word("WHERE")
            filtered = self.expression()
            self.expect_symbol(")")
        if (distinct or order is not None or filtered is not None) and name not in AGGREGATES:
            raise SqlSyntaxError("DISTINCT, WITHIN GROUP and FILTER apply to aggregates only")
        if (order is not None) != (name in ("PERCENTILE_CONT", "PERCENTILE_DISC")):
            raise SqlSyntaxError("WITHIN GROUP belongs to PERCENTILE_CONT and PERCENTILE_DISC")
        call = Call(name, tuple(arguments), distinct, filtered, order, descending)
        if self.word("OVER"):
            self.at += 1
            return self.window(call)
        if name in WINDOW_ONLY:
            raise SqlSyntaxError("a window function needs OVER")
        return call

    def window(self, call: Call) -> Window:
        self.expect_symbol("(")
        partition: list[Node] = []
        order: list[tuple[Node, bool]] = []
        frame: list[str] = []
        if self.word("PARTITION"):
            self.at += 1
            self.expect_word("BY")
            partition.append(self.expression())
            while self.symbol(","):
                self.at += 1
                partition.append(self.expression())
        if self.word("ORDER"):
            self.at += 1
            self.expect_word("BY")
            while True:
                key = self.expression()
                descending = False
                if self.word("ASC", "DESC"):
                    descending = self.take().upper == "DESC"
                if self.word("NULLS"):
                    self.at += 1
                    if not self.word("FIRST", "LAST"):
                        raise SqlSyntaxError("NULLS expects FIRST or LAST")
                    self.at += 1
                order.append((key, descending))
                if not self.symbol(","):
                    break
                self.at += 1
        if self.word("ROWS", "RANGE", "GROUPS"):
            while not self.symbol(")"):
                token = self.take()
                if token.kind not in ("word", "number"):
                    raise SqlSyntaxError("a window frame holds words and numbers")
                frame.append(token.upper or token.text)
        self.expect_symbol(")")
        return Window(call, tuple(partition), tuple(order), tuple(frame))


def parse(text: str) -> Node:
    """One OSSIE_SQL_2026 expression as a tree."""
    parser = _Parser(text)
    if not parser.tokens:
        raise SqlSyntaxError("the expression is empty")
    node = parser.expression()
    if parser.peek() is not None:
        raise SqlSyntaxError("text follows the expression")
    return node


# --- reading a tree ---------------------------------------------------------------------------


def children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, Unary | Not):
        return (node.operand,)
    if isinstance(node, Binary | IsDistinct):
        return (node.left, node.right)
    if isinstance(node, Between):
        return (node.operand, node.low, node.high)
    if isinstance(node, InList):
        return (node.operand, *node.items)
    if isinstance(node, Like):
        return (node.operand, node.pattern) + ((node.escape,) if node.escape else ())
    if isinstance(node, IsNull | Cast):
        return (node.operand,)
    if isinstance(node, Case):
        found: list[Node] = [] if node.operand is None else [node.operand]
        for condition, result in node.whens:
            found += [condition, result]
        return tuple(found) + ((node.otherwise,) if node.otherwise is not None else ())
    if isinstance(node, Call):
        extra = tuple(n for n in (node.filter, node.order) if n is not None)
        return node.arguments + extra
    if isinstance(node, Window):
        return (node.call, *node.partition, *(key for key, _ in node.order))
    return ()


def walk(node: Node) -> list[Node]:
    """Every node, depth first, in source order."""
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        found.append(current)
        stack.extend(reversed(children(current)))
    return found


def references(node: Node) -> list[Ref]:
    return [n for n in walk(node) if isinstance(n, Ref)]


def unsupported(node: Node) -> str | None:
    """The first construct of a valid tree the product does not implement, as a fixed
    operation name: windows are declared with `window` (S9); EXPERIMENTAL functions; functions
    outside the subset's catalog."""
    for current in walk(node):
        if isinstance(current, Window):
            return "window_function"
        if isinstance(current, Call):
            if current.name == "TO_CHAR" or (
                current.name in ("TO_DATE", "TO_TIMESTAMP") and len(current.arguments) > 1
            ):
                return "experimental_function"
            if current.name not in AGGREGATES and current.name not in SCALARS:
                return "unknown_function"
    return None


def aggregate_class(node: Node) -> AggregateClass | None:
    """The most restrictive decomposability class among the aggregates (L281-L290), or None
    when there is none. A DISTINCT aggregate other than MIN or MAX is holistic."""
    found: list[AggregateClass] = []
    for current in walk(node):
        if isinstance(current, Call) and current.name in AGGREGATES:
            kind = AGGREGATES[current.name]
            if current.distinct and current.name not in ("MIN", "MAX"):
                kind = "holistic"
            found.append(kind)
    if not found:
        return None
    return max(found, key=AGGREGATE_CLASSES.index)
