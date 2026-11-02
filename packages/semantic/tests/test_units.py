"""P2-04d (#290; #280 is the source of truth): the units registry, the ISO 4217 snapshot and
UCUM syntax."""

from __future__ import annotations

import csv
import hashlib
import tomllib
from fractions import Fraction
from pathlib import Path

import pytest
from orvex.semantic.compiler import units
from orvex.semantic.compiler.units import MAX_EXPONENT, ONE, UnitError, currency, parse

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "vendor" / "iso-4217"
SNAPSHOT = ROOT / "packages/semantic/src/orvex/semantic/compiler/registries/iso4217.csv"


def test_the_vendored_iso_4217_files_match_their_hashes() -> None:
    pin = tomllib.loads((VENDOR / "PIN.toml").read_text("utf-8"))
    for name, digest in pin["sha256"].items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_the_snapshot_is_the_active_codes_of_the_vendored_list() -> None:
    pin = tomllib.loads((VENDOR / "PIN.toml").read_text("utf-8"))
    rows = csv.DictReader((VENDOR / "codes-all.csv").read_text("utf-8").splitlines())
    active: dict[str, tuple[str, str]] = {}
    for row in rows:
        if row["AlphabeticCode"] and not row["WithdrawalDate"]:
            active.setdefault(row["AlphabeticCode"], (row["NumericCode"], row["MinorUnit"]))
    expected = sorted(
        (code, *shape) for code, shape in active.items() if code not in pin["excluded"]
    )
    snapshot = csv.DictReader(SNAPSHOT.read_text("utf-8").splitlines())
    assert [(r["code"], r["numeric"], r["minor_unit"]) for r in snapshot] == expected
    assert {"USD", "BOB", "EUR"} <= units.registry().currencies
    assert not {"XXX", "XTS", "ZZZ"} & units.registry().currencies


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("kW.h", "h.kW"),
        ("kW.h", "W.h.10*3"),
        ("kJ", "kW.s"),
        ("L", "dm3"),
        ("l", "L"),
        ("t", "Mg"),
        ("BOB/USD.USD", "BOB"),
        ("N.m", "J"),
        ("/s", "Hz"),
        ("s-1", "Hz"),
        ("mo_j", "a_j/12"),
        ("{customer}", "1"),
        ("%", "10*-2"),
        ("m2/s2", "m2.s-2"),
    ],
)
def test_equal_units_are_equal_whatever_their_spelling(left: str, right: str) -> None:
    assert parse(left) == parse(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [("km", "m"), ("kW.h", "kW"), ("USD", "BOB"), ("L", "m3"), ("%", "1"), ("g", "kg")],
)
def test_no_conversion_makes_two_units_equal(left: str, right: str) -> None:
    assert parse(left) != parse(right)


def test_factors_are_exact() -> None:
    assert parse("kW.h").factor == 3_600_000_000  # grams, metres, seconds
    assert parse("mo_j").factor == Fraction(36525 * 86400, 1200)
    assert parse("[ppm]").factor == Fraction(1, 1_000_000)
    assert currency("USD") * parse("BOB/USD") == currency("BOB")
    assert parse("m2").root() == parse("m")
    assert parse("m3").root() is None
    assert ONE.text == "1"


@pytest.mark.parametrize(
    "text",
    [
        "xyz",
        "ZZZ",
        "XXX",
        "kh",  # an hour takes no prefix
        "mUSD",
        "Cel",  # an interval scale
        "deg",  # not a rational factor
        "",
        "k W",
        "m-",
        "(m",
        "m)",
        "{a",
        "m.",
        "x" * 65,
        "(m/s)2",  # UCUM puts no exponent on a parenthesis
        "(m){x}",  # nor an annotation
        "m{}",
        "m{\x01}",  # annotations are printable ASCII
        "m{é}",
        "m0",
        "0",  # a factor of zero measures nothing
        "m/0",
        "km13",  # beyond the exponent bound
        "km999999999",
        "10*999999999999",
        "10*61",
    ],
)
def test_unknown_units_are_refused(text: str) -> None:
    with pytest.raises(UnitError):
        parse(text)


def test_computed_units_stay_bounded_and_their_text_short() -> None:
    unit = parse("km")
    for _ in range(MAX_EXPONENT - 1):
        unit = unit * parse("km")
    assert len(unit.text) <= 300
    with pytest.raises(UnitError):
        unit * parse("km")
    with pytest.raises(UnitError):
        parse("km").power(999_999_999_999)
    assert (parse("km") * parse("h")).text == "km.h"
    assert parse("km").power(2).text == "km2"
    assert (parse("USD") * parse("BOB/USD")).text == "BOB"


def test_versions_identify_the_registry_and_the_snapshot() -> None:
    registry_version, snapshot = units.versions()
    assert registry_version == "units/1"
    digest = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    assert snapshot == f"iso4217/sha256:{digest}"
