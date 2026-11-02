"""Units and currencies by dimensional analysis (P2-04d, #290; #280 is the source of truth).

A `Unit` is an exact rational factor times a product of dimensions with integer exponents: the
base units of `registries/units.toml`, and each ISO 4217 currency as a dimension of its own
(`registries/iso4217.csv`, vendor/iso-4217). Two units are equal when both the dimensions and the
factor are equal; nothing is ever converted (#280 agent decision 2), so `km` and `m` differ.

`parse(text)` reads the case-sensitive syntax of UCUM:

    term       := ["/"] component (("." | "/") component)*
    component  := "(" term ")" [annotation] | simple [exponent] [annotation] | annotation
                | integer | "10*" exponent | "10^" exponent
    simple     := atom | prefix atom | currency
    annotation := "{" text without braces "}"   (meaning 1)

`.` and `/` apply left to right, as UCUM defines. A three-letter upper-case code from the ISO 4217
snapshot is a currency and takes no prefix or exponent restrictions beyond the grammar.
"""

from __future__ import annotations

import csv
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from functools import cache
from importlib import resources
from math import isqrt

MAX_TEXT = 64


class UnitError(ValueError):
    """Not a unit of the registry or the snapshot."""


@dataclass(frozen=True, slots=True)
class Unit:
    """`dimensions` is sorted, with no zero exponent; `text` is how the unit is shown, which
    never takes part in equality."""

    factor: Fraction
    dimensions: tuple[tuple[str, int], ...]
    text: str = field(default="", compare=False)

    def __mul__(self, other: Unit) -> Unit:
        return _make(self.factor * other.factor, _combine(self, other, 1), _join(self, other, "."))

    def __truediv__(self, other: Unit) -> Unit:
        return _make(self.factor / other.factor, _combine(self, other, -1), _join(self, other, "/"))

    def power(self, exponent: int) -> Unit:
        dimensions = {name: power * exponent for name, power in self.dimensions}
        text = self.text if exponent == 1 else f"({self.text}){exponent}"
        return _make(self.factor**exponent, dimensions, text)

    def root(self) -> Unit | None:
        """The square root, when it is a unit (even exponents, a square factor)."""
        if any(power % 2 for _, power in self.dimensions):
            return None
        numerator, denominator = (
            _square_root(self.factor.numerator),
            _square_root(self.factor.denominator),
        )
        if numerator is None or denominator is None:
            return None
        dimensions = {name: power // 2 for name, power in self.dimensions}
        return _make(Fraction(numerator, denominator), dimensions, f"sqrt({self.text})")


def _square_root(value: int) -> int | None:
    root = isqrt(value)
    return root if root * root == value else None


def _combine(left: Unit, right: Unit, sign: int) -> dict[str, int]:
    dimensions = dict(left.dimensions)
    for name, power in right.dimensions:
        dimensions[name] = dimensions.get(name, 0) + sign * power
    return dimensions


def _join(left: Unit, right: Unit, operator: str) -> str:
    def wrap(unit: Unit) -> str:
        return f"({unit.text})" if any(c in unit.text for c in "./") else unit.text

    return f"{wrap(left)}{operator}{wrap(right)}"


def _canonical(factor: Fraction, dimensions: tuple[tuple[str, int], ...]) -> str | None:
    """UCUM text of a unit whose factor is 1: `BOB/USD`, `m2`, `1`."""
    if factor != 1:
        return None
    above = [f"{name}{power if power != 1 else ''}" for name, power in dimensions if power > 0]
    below = [f"{name}{-power if power != -1 else ''}" for name, power in dimensions if power < 0]
    text = ".".join(above) or "1"
    return text + "".join(f"/{name}" for name in below)


def _make(factor: Fraction, dimensions: Mapping[str, int], text: str) -> Unit:
    kept = tuple(sorted((name, power) for name, power in dimensions.items() if power != 0))
    return Unit(factor, kept, _canonical(factor, kept) or text)


ONE = Unit(Fraction(1), (), "1")


# --- the registry and the snapshot ---------------------------------------------------------------


@dataclass(frozen=True)
class _Registry:
    version: int
    prefixes: Mapping[str, Fraction]
    atoms: Mapping[str, tuple[Unit, bool]]  # code -> (unit, metric)
    currencies: frozenset[str]


@cache
def registry() -> _Registry:
    data = resources.files("orvex.semantic.compiler").joinpath("registries")
    table = tomllib.loads(data.joinpath("units.toml").read_text("utf-8"))
    rows = data.joinpath("iso4217.csv").read_text("utf-8").splitlines()
    currencies = frozenset(row["code"] for row in csv.DictReader(rows))
    prefixes = {code: Fraction(Decimal(value)) for code, value in table["prefixes"].items()}
    atoms: dict[str, tuple[Unit, bool]] = {
        code: (Unit(Fraction(1), ((code, 1),), code), True) for code in table["base"]
    }
    partial = _Registry(table["version"], prefixes, atoms, currencies)
    for code, entry in table["units"].items():
        defined = _Parser(str(entry["unit"]), partial).parse()
        unit = Unit(
            Fraction(Decimal(str(entry["value"]))) * defined.factor, defined.dimensions, code
        )
        atoms[code] = (unit, bool(entry.get("metric", False)))
    return partial


def currency(code: str) -> Unit:
    if code not in registry().currencies:
        raise UnitError(code)
    return Unit(Fraction(1), ((code, 1),), code)


def is_currency(code: str) -> bool:
    return code in registry().currencies


# --- the parser ----------------------------------------------------------------------------------

_SIMPLE = re.compile(r"(?P<name>\[[^\[\]]+\]|[^.\/(){}\[\]0-9+-]+)(?P<exponent>[+-]?[0-9]+)?")
_INTEGER = re.compile(r"[0-9]+")
_TEN = re.compile(r"10[*^](?P<exponent>[+-]?[0-9]+)")
_ANNOTATION = re.compile(r"\{[^{}]*\}")


class _Parser:
    def __init__(self, text: str, table: _Registry) -> None:
        self.text = text
        self.at = 0
        self.table = table

    def parse(self) -> Unit:
        if not self.text or len(self.text) > MAX_TEXT or any(c.isspace() for c in self.text):
            raise UnitError(self.text)
        unit = self.term()
        if self.at != len(self.text):
            raise UnitError(self.text)
        return Unit(unit.factor, unit.dimensions, self.text)

    def term(self) -> Unit:
        if self.peek("/"):
            self.at += 1
            unit = ONE / self.component()
        else:
            unit = self.component()
        while self.peek(".") or self.peek("/"):
            operator = self.text[self.at]
            self.at += 1
            following = self.component()
            unit = unit * following if operator == "." else unit / following
        return unit

    def peek(self, symbol: str) -> bool:
        return self.text.startswith(symbol, self.at)

    def component(self) -> Unit:
        if self.peek("("):
            self.at += 1
            unit = self.term()
            if not self.peek(")"):
                raise UnitError(self.text)
            self.at += 1
            self.annotation()
            return unit
        if self.peek("{"):
            self.annotation(required=True)
            return ONE
        ten = _TEN.match(self.text, self.at)
        if ten is not None:
            self.at = ten.end()
            return Unit(Fraction(10) ** int(ten.group("exponent")), (), ten.group(0))
        integer = _INTEGER.match(self.text, self.at)
        if integer is not None:
            self.at = integer.end()
            return Unit(Fraction(int(integer.group(0))), (), integer.group(0))
        simple = _SIMPLE.match(self.text, self.at)
        if simple is None:
            raise UnitError(self.text)
        self.at = simple.end()
        unit = self.simple(simple.group("name"))
        exponent = simple.group("exponent")
        if exponent is not None:
            unit = unit.power(int(exponent))
        self.annotation()
        return unit

    def annotation(self, *, required: bool = False) -> None:
        found = _ANNOTATION.match(self.text, self.at)
        if found is None:
            if required:
                raise UnitError(self.text)
            return
        if not found.group(0)[1:-1].isascii():
            raise UnitError(self.text)
        self.at = found.end()

    def simple(self, name: str) -> Unit:
        """An atom, a prefixed metric atom, or a currency; an exact atom wins over a prefix."""
        if name in self.table.atoms:
            return self.table.atoms[name][0]
        if name in self.table.currencies:
            return Unit(Fraction(1), ((name, 1),), name)
        for size in (2, 1):
            prefix, rest = name[:size], name[size:]
            if prefix in self.table.prefixes and rest in self.table.atoms:
                unit, metric = self.table.atoms[rest]
                if metric:
                    return Unit(self.table.prefixes[prefix] * unit.factor, unit.dimensions, name)
        raise UnitError(name)


def parse(text: str) -> Unit:
    """The unit of a declared UCUM text, or `UnitError`."""
    return _Parser(text, registry()).parse()


def units_version() -> int:
    return registry().version
