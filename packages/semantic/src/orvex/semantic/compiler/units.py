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
import hashlib
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
# Bounds that keep every unit small and every shown text within the error contract (a `Name`
# holds at most 300 characters): no real measure comes near them.
MAX_EXPONENT = 12
MAX_DIMENSIONS = 8
MAX_FACTOR_DIGITS = 60


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
        largest = max((abs(power) for _, power in self.dimensions), default=0)
        digits = max(len(str(self.factor.numerator)), len(str(self.factor.denominator)))
        if largest * abs(exponent) > MAX_EXPONENT or digits * abs(exponent) > MAX_FACTOR_DIGITS + 1:
            raise UnitError(self.text)
        dimensions = {name: power * exponent for name, power in self.dimensions}
        simple = not any(c in self.text for c in "./(){}") and not self.text[-1:].isdigit()
        text = self.text if exponent == 1 else f"{self.text}{exponent}" if simple else ""
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


def _canonical(factor: Fraction, dimensions: tuple[tuple[str, int], ...]) -> str:
    """UCUM text of any unit, from its dimensions: `BOB/USD`, `m2`, `1`, `1000.m`, `m3/1000`."""
    above = [f"{name}{power if power != 1 else ''}" for name, power in dimensions if power > 0]
    below = [f"{name}{-power if power != -1 else ''}" for name, power in dimensions if power < 0]
    if factor.numerator != 1:
        above.insert(0, str(factor.numerator))
    if factor.denominator != 1:
        below.append(str(factor.denominator))
    return (".".join(above) or "1") + "".join(f"/{name}" for name in below)


def _make(factor: Fraction, dimensions: Mapping[str, int], text: str) -> Unit:
    """A computed unit, within the bounds; shown as its operands wrote it when that is short,
    and in canonical form otherwise (always when its factor is 1)."""
    kept = tuple(sorted((name, power) for name, power in dimensions.items() if power != 0))
    if (
        factor <= 0
        or len(kept) > MAX_DIMENSIONS
        or any(abs(power) > MAX_EXPONENT for _, power in kept)
        or len(str(factor.numerator)) > MAX_FACTOR_DIGITS
        or len(str(factor.denominator)) > MAX_FACTOR_DIGITS
    ):
        raise UnitError(text or "unit")
    if factor == 1 or not text or len(text) > MAX_TEXT:
        text = _canonical(factor, kept)
    return Unit(factor, kept, text)


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


# --- the parser ----------------------------------------------------------------------------------

_SIMPLE = re.compile(r"(?P<name>\[[^\[\]]+\]|[^.\/(){}\[\]0-9+-]+)(?P<exponent>[+-]?[0-9]+)?")
_INTEGER = re.compile(r"[0-9]{1,60}(?![0-9])")
_TEN = re.compile(r"10[*^](?P<exponent>[+-]?[0-9]+)")
_ANNOTATION = re.compile(r"\{[!-z|~]+\}")  # printable ASCII but braces, as UCUM defines


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
            return unit  # UCUM puts no exponent or annotation on a parenthesis
        if self.peek("{"):
            self.annotation(required=True)
            return ONE
        ten = _TEN.match(self.text, self.at)
        if ten is not None:
            self.at = ten.end()
            power = int(ten.group("exponent"))
            if abs(power) > MAX_FACTOR_DIGITS:
                raise UnitError(self.text)
            return Unit(Fraction(10) ** power, (), ten.group(0))
        integer = _INTEGER.match(self.text, self.at)
        if integer is not None:
            self.at = integer.end()
            if int(integer.group(0)) == 0:
                raise UnitError(self.text)  # a factor of zero measures nothing
            return Unit(Fraction(int(integer.group(0))), (), integer.group(0))
        simple = _SIMPLE.match(self.text, self.at)
        if simple is None:
            raise UnitError(self.text)
        self.at = simple.end()
        unit = self.simple(simple.group("name"))
        exponent = simple.group("exponent")
        if exponent is not None:
            if int(exponent) == 0 or abs(int(exponent)) > MAX_EXPONENT:
                raise UnitError(self.text)
            unit = unit.power(int(exponent))
        self.annotation()
        return unit

    def annotation(self, *, required: bool = False) -> None:
        found = _ANNOTATION.match(self.text, self.at)
        if found is None:
            if required:
                raise UnitError(self.text)
            return
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


def versions() -> tuple[str, str]:
    """The registry's version and the snapshot's identity (the SHA-256 of its file), for the
    model fingerprint (P2-04e)."""
    data = resources.files("orvex.semantic.compiler").joinpath("registries")
    snapshot = hashlib.sha256(data.joinpath("iso4217.csv").read_bytes()).hexdigest()
    return f"units/{registry().version}", f"iso4217/sha256:{snapshot}"
