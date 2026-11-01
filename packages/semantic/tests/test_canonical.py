"""P2-04a (#287): canonical JSON (RFC 8785) and fingerprints."""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import struct
import subprocess
import sys
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from orvex.semantic.canonical import (
    MAX_SAFE_INTEGER,
    NotCanonical,
    canonicalize,
    canonicalize_text,
    fingerprint,
    number,
)

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "vendor" / "json-canonicalization"
PIN = tomllib.loads((VENDOR / "PIN.toml").read_text("utf-8"))
CASES = ("arrays", "french", "structures", "unicode", "values", "weird")


def double(hex_ieee: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(hex_ieee.rjust(16, "0")))[0]
    return value


def test_every_vendored_file_matches_its_pin() -> None:
    assert PIN["commit"] == "32a48195b9b20fe67b1795e39e4509f14d860b45"
    files = {str(p.relative_to(VENDOR)) for p in VENDOR.rglob("*") if p.is_file()} - {"PIN.toml"}
    assert files == set(PIN["sha256"])
    for name, digest in PIN["sha256"].items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize("case", CASES)
def test_upstream_files_canonicalize_byte_exact(case: str) -> None:
    """The six upstream pairs. `values` is RFC 8785 §3.2.2's example, `weird` §3.2.3's sorting
    example, and its byte dump `outhex/values.txt` is §3.2.4's."""
    produced = canonicalize_text((VENDOR / "input" / f"{case}.json").read_text("utf-8"))
    assert produced == (VENDOR / "output" / f"{case}.json").read_bytes()
    assert produced == bytes.fromhex((VENDOR / "outhex" / f"{case}.txt").read_text("ascii"))


# RFC 8785 Appendix B: IEEE-754 value, expected serialization. Each expected value was also
# confirmed against an ECMAScript engine's Number.prototype.toString.
APPENDIX_B = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize(("hex_ieee", "expected"), APPENDIX_B)
def test_appendix_b_numbers(hex_ieee: str, expected: str) -> None:
    assert number(double(hex_ieee)) == expected


@pytest.mark.parametrize("hex_ieee", ["7fffffffffffffff", "7ff0000000000000", "fff0000000000000"])
def test_nan_and_infinity_are_refused(hex_ieee: str) -> None:
    with pytest.raises(NotCanonical):
        number(double(hex_ieee))


def test_the_pinned_slice_of_the_es6_number_file() -> None:
    lines = gzip.decompress((VENDOR / "es6testfile-first-10000.txt.gz").read_bytes()).decode()
    rows = lines.splitlines()
    assert len(rows) == PIN["number_file_lines"]
    for row in rows:
        hex_ieee, expected = row.split(",")
        assert number(double(hex_ieee)) == expected, hex_ieee


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_numbers_round_trip_with_the_fewest_digits(value: float) -> None:
    text = number(value)
    assert float(text) == value
    if value != 0:
        mantissa = text.lstrip("-").split("e")[0].replace(".", "").strip("0")
        if len(mantissa) > 1:
            shorter = float(f"{value:.{len(mantissa) - 2}e}")
            assert shorter != value  # one digit fewer does not round-trip


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1, "a": 2}',
        '{"\\ud800": 1}',
        '["\\udc00"]',
        "[NaN]",
        "[Infinity]",
        "[-Infinity]",
        "[1e400]",
        "[1] [2]",
        "{'a': 1}",
    ],
)
def test_text_that_is_not_i_json_is_refused(text: str) -> None:
    with pytest.raises(NotCanonical):
        canonicalize_text(text)


@pytest.mark.parametrize(
    "value",
    [
        MAX_SAFE_INTEGER + 1,
        -(MAX_SAFE_INTEGER + 1),
        Decimal("1.10"),
        {1: "a"},
        b"bytes",
        {"x": {1, 2}},
        "\ud800",
        math.nan,
    ],
)
def test_values_without_a_canonical_form_are_refused(value: object) -> None:
    with pytest.raises(NotCanonical):
        canonicalize(value)


def test_plain_values() -> None:
    assert canonicalize({"b": [True, None, MAX_SAFE_INTEGER], "a": "é\u001f"}) == (
        '{"a":"é\\u001f","b":[true,null,9007199254740991]}'.encode()
    )
    assert canonicalize({"\U0001f600": 1, "￿": 2}) == '{"\U0001f600":1,"￿":2}'.encode()


def test_fingerprint_shape_and_tagging() -> None:
    first = fingerprint("model", 1, {"a": "1.10"})
    assert first.startswith("sha256:")
    assert len(first) == 7 + 64
    assert (
        first
        == "sha256:"
        + hashlib.sha256(b'{"format":1,"kind":"model","value":{"a":"1.10"}}').hexdigest()
    )
    assert fingerprint("member", 1, {"a": "1.10"}) != first
    assert fingerprint("model", 2, {"a": "1.10"}) != first


_SCRIPT = (
    "from orvex.semantic.canonical import fingerprint\n"
    "doc = {f'k{i}': [i, str(i), {'z': None, 'a': i % 2 == 0}] for i in range(200)}\n"
    "print(fingerprint('model', 1, doc))\n"
)


def test_fingerprints_are_equal_across_processes_and_hash_seeds() -> None:
    outputs = set()
    for seed in ("0", "1", "4242", "random"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1
    # A literal anchors the value across Python versions: CI runs this file on both.
    assert outputs == {_EXPECTED}


# Computed independently with an ECMAScript engine (sorted keys, JSON.stringify, SHA-256).
_EXPECTED = "sha256:194f4d16a6e6423a8948578ae43cb02f07e820b63fc14d3aee4de9abfee7b1e1"
