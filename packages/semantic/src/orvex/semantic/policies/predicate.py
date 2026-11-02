"""The closed predicate grammar of `security-policies` v1 (P2-08, #304).

A row predicate (and a `visible_when`) reads only:
- the fields of its policy's dataset;
- `$attribute` parameters, always bound and never text;
- string and integer literals.

It combines them with comparisons, `IS [NOT] NULL`, `AND`, `OR`, `NOT` and parentheses, and with:
- `field [NOT] IN ($attribute)`, over a list attribute;
- `field [NOT] IN (SELECT column FROM schema.table WHERE condition AND ...)`;
- `[NOT] EXISTS (SELECT 1 FROM schema.table WHERE condition AND ...)`.

A condition is an equality between a column of that table and a field (qualified with the
dataset's name), an attribute or a literal. Anything else — a function call, an operator
outside the list, a second table, a non-equality inside a subquery, a comment, a semicolon,
string concatenation — is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

COMPARISONS = frozenset({"=", "<>", "!=", "<", "<=", ">", ">="})
KEYWORDS = frozenset(
    {"AND", "OR", "NOT", "IN", "EXISTS", "SELECT", "FROM", "WHERE", "IS", "NULL", "TRUE", "FALSE"}
)
_TOKEN = re.compile(
    r"""\s*(?:
    (?P<attr>\$[a-z][a-z0-9_]{0,62}\b)
  | (?P<string>'(?:[^']|'')*')
  | (?P<number>-?[0-9]+\b)
  | (?P<quoted>"(?:[^"]|"")+")
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|<>|!=|=|<|>)
  | (?P<punct>[(),.])
  )""",
    re.VERBOSE,
)


class PredicateError(ValueError):
    """A predicate outside the grammar. The message names the construct, never a value."""


@dataclass(frozen=True, slots=True)
class Field:
    name: str  # normalized


@dataclass(frozen=True, slots=True)
class Attribute:
    name: str


@dataclass(frozen=True, slots=True)
class Value:
    kind: Literal["string", "integer", "boolean"]
    text: str


Operand = Field | Attribute | Value


@dataclass(frozen=True, slots=True)
class Compare:
    left: Operand
    op: str
    right: Operand


@dataclass(frozen=True, slots=True)
class IsNull:
    field: Field
    negated: bool


@dataclass(frozen=True, slots=True)
class Condition:
    column: str  # a column of the subquery's table
    value: Operand


@dataclass(frozen=True, slots=True)
class InAttribute:
    field: Field
    attribute: Attribute
    negated: bool


@dataclass(frozen=True, slots=True)
class InSelect:
    field: Field
    table: str
    column: str
    conditions: tuple[Condition, ...]
    negated: bool


@dataclass(frozen=True, slots=True)
class Exists:
    table: str
    conditions: tuple[Condition, ...]
    negated: bool


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class Logical:
    op: Literal["AND", "OR"]
    operands: tuple[Node, ...]


Node = Compare | IsNull | InAttribute | InSelect | Exists | Not | Logical
MAX_DEPTH = 32


def _normalize(name: str) -> str:
    if name.startswith('"'):
        return name[1:-1].replace('""', '"')
    return name.upper()


def _tokens(text: str) -> list[tuple[str, str]]:
    if any(mark in text for mark in ("--", "/*", "*/", ";", "||")):
        raise PredicateError("comments, statement separators and concatenation are refused")
    found: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise PredicateError("a character outside the grammar")
        kind = match.lastgroup
        assert kind is not None
        value = match.group(kind)
        if kind == "word" and value.upper() in KEYWORDS:
            kind, value = "keyword", value.upper()
        found.append((kind, value))
        position = match.end()
    return found


class _Parser:
    def __init__(self, text: str, dataset: str) -> None:
        self.tokens = _tokens(text)
        self.at = 0
        self.dataset = dataset  # normalized: the qualifier of outer fields inside a subquery
        self.depth = 0

    def peek(self, offset: int = 0) -> tuple[str, str]:
        index = self.at + offset
        return self.tokens[index] if index < len(self.tokens) else ("end", "")

    def take(self, kind: str, value: str | None = None) -> str:
        found_kind, found = self.peek()
        if found_kind != kind or (value is not None and found != value):
            raise PredicateError(f"expected {value or kind}")
        self.at += 1
        return found

    def keyword(self, value: str) -> bool:
        if self.peek() == ("keyword", value):
            self.at += 1
            return True
        return False

    def expression(self) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise PredicateError("nested too deeply")
        operands = [self.conjunction()]
        while self.keyword("OR"):
            operands.append(self.conjunction())
        self.depth -= 1
        return operands[0] if len(operands) == 1 else Logical("OR", tuple(operands))

    def conjunction(self) -> Node:
        operands = [self.negation()]
        while self.keyword("AND"):
            operands.append(self.negation())
        return operands[0] if len(operands) == 1 else Logical("AND", tuple(operands))

    def negation(self) -> Node:
        if self.peek() == ("keyword", "NOT") and self.peek(1) != ("keyword", "EXISTS"):
            self.at += 1
            return Not(self.negation())
        return self.primary()

    def primary(self) -> Node:
        if self.peek() == ("punct", "("):
            self.at += 1
            node = self.expression()
            self.take("punct", ")")
            return node
        if self.peek() in {("keyword", "EXISTS"), ("keyword", "NOT")}:
            negated = self.keyword("NOT")
            self.take("keyword", "EXISTS")
            self.take("punct", "(")
            self.take("keyword", "SELECT")
            if self.take("number") != "1":
                raise PredicateError("EXISTS selects 1")
            table, conditions = self.subquery_rest()
            return Exists(table, conditions, negated)
        left = self.operand(outer=True)
        if self.keyword("IS"):
            negated = self.keyword("NOT")
            self.take("keyword", "NULL")
            if not isinstance(left, Field):
                raise PredicateError("IS NULL applies to a field")
            return IsNull(left, negated)
        negated = False
        if self.peek() == ("keyword", "NOT") and self.peek(1) == ("keyword", "IN"):
            self.at += 1
            negated = True
        if self.keyword("IN"):
            if not isinstance(left, Field):
                raise PredicateError("IN applies to a field")
            self.take("punct", "(")
            if self.peek()[0] == "attr":
                attribute = Attribute(self.take("attr")[1:])
                self.take("punct", ")")
                return InAttribute(left, attribute, negated)
            self.take("keyword", "SELECT")
            column = self.column()
            table, conditions = self.subquery_rest()
            return InSelect(left, table, column, conditions, negated)
        kind, op = self.peek()
        if kind != "op" or op not in COMPARISONS:
            raise PredicateError("expected a comparison")
        self.at += 1
        right = self.operand(outer=True)
        if not (isinstance(left, Field) or isinstance(right, Field)):
            raise PredicateError("a comparison reads a field")
        return Compare(left, op, right)

    def subquery_rest(self) -> tuple[str, tuple[Condition, ...]]:
        self.take("keyword", "FROM")
        schema = self.take("word")
        self.take("punct", ".")
        table = f"{schema}.{self.take('word')}"
        conditions: list[Condition] = []
        if self.keyword("WHERE"):
            conditions.append(self.condition(table))
            while self.keyword("AND"):
                conditions.append(self.condition(table))
        self.take("punct", ")")
        return table, tuple(conditions)

    def column(self) -> str:
        """A column of the subquery's table: bare, or qualified by that table's name."""
        first = self.take("word")
        if self.peek() == ("punct", "."):
            self.at += 1
            return self.take("word").lower()
        return first.lower()

    def condition(self, table: str) -> Condition:
        left = self.side(table)
        self.take("op", "=")
        sides = [left, self.side(table)]
        columns = [side for side in sides if isinstance(side, str)]
        others = [side for side in sides if side is not None and not isinstance(side, str)]
        if len(columns) != 1 or len(others) != 1:
            raise PredicateError("a condition is one column of the table equal to one value")
        return Condition(columns[0], others[0])

    def side(self, table: str) -> str | Operand:
        kind, value = self.peek()
        if kind in {"attr", "string", "number"} or (
            kind == "keyword" and value in {"TRUE", "FALSE"}
        ):
            return self.operand(outer=False)
        if kind not in {"word", "quoted"}:
            raise PredicateError("expected a column, a field or a value")
        self.at += 1
        if self.peek() == ("punct", "."):
            self.at += 1
            name = self.take("word") if self.peek()[0] == "word" else self.take("quoted")
            if _normalize(value) == self.dataset:
                return Field(_normalize(name))
            if value.lower() != table.split(".")[1].lower():
                raise PredicateError("a subquery reads one table")
            return name.lower()
        return value.lower()

    def operand(self, outer: bool) -> Operand:
        kind, value = self.peek()
        self.at += 1
        if kind == "attr":
            return Attribute(value[1:])
        if kind == "string":
            if "$" in value:
                raise PredicateError("an attribute is never written inside a string")
            return Value("string", value[1:-1].replace("''", "'"))
        if kind == "number":
            return Value("integer", value)
        if kind == "keyword" and value in {"TRUE", "FALSE"}:
            return Value("boolean", value.lower())
        if kind in {"word", "quoted"} and outer:
            if self.peek() == ("punct", "("):
                raise PredicateError("function calls are refused")
            if self.peek() == ("punct", "."):
                raise PredicateError("an outer field is not qualified")
            return Field(_normalize(value))
        raise PredicateError("expected a field, an attribute or a literal")


def parse(text: str, dataset: str) -> Node:
    """The predicate of `text` for the dataset `dataset` (normalized), or `PredicateError`."""
    parser = _Parser(text, dataset)
    node = parser.expression()
    if parser.peek()[0] != "end":
        raise PredicateError("text after the predicate")
    return node


def walk(node: Node) -> list[Node]:
    found: list[Node] = [node]
    if isinstance(node, Not):
        found += walk(node.operand)
    elif isinstance(node, Logical):
        for operand in node.operands:
            found += walk(operand)
    return found


def fields(node: Node) -> set[str]:
    found: set[str] = set()
    for item in walk(node):
        operands: list[object] = []
        if isinstance(item, Compare):
            operands = [item.left, item.right]
        elif isinstance(item, IsNull | InAttribute):
            operands = [item.field]
        elif isinstance(item, InSelect):
            operands = [item.field, *(c.value for c in item.conditions)]
        elif isinstance(item, Exists):
            operands = [c.value for c in item.conditions]
        found |= {operand.name for operand in operands if isinstance(operand, Field)}
    return found


def attributes(node: Node) -> set[str]:
    found: set[str] = set()
    for item in walk(node):
        operands: list[object] = []
        if isinstance(item, Compare):
            operands = [item.left, item.right]
        elif isinstance(item, InAttribute):
            operands = [item.attribute]
        elif isinstance(item, InSelect | Exists):
            operands = [c.value for c in item.conditions]
        found |= {operand.name for operand in operands if isinstance(operand, Attribute)}
    return found


def tables(node: Node) -> list[tuple[str, set[str]]]:
    """Every mapping table read, with the columns it reads."""
    found = []
    for item in walk(node):
        if isinstance(item, InSelect):
            found.append((item.table, {item.column, *(c.column for c in item.conditions)}))
        elif isinstance(item, Exists):
            found.append((item.table, {c.column for c in item.conditions}))
    return found


def deny_list(node: Node, negated: bool = False) -> bool:
    """Whether a mapping-table subquery is read under negation: a deny-list (`NOT IN`,
    `NOT EXISTS`, or one under `NOT`), which needs an explicit registration (ADR-0027 §6)."""
    if isinstance(node, Not):
        return deny_list(node.operand, not negated)
    if isinstance(node, Logical):
        return any(deny_list(operand, negated) for operand in node.operands)
    if isinstance(node, InSelect | Exists):
        return node.negated != negated
    return False
