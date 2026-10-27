"""P1-03: exact DuckDB -> Arrow -> JSON encoding (contract store v1, ResultJson)."""

from __future__ import annotations

import decimal
import json

import duckdb
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.contracts.store.v1 import ResultJson
from orvex.store.errors import StoreError
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import _with_duckdb_types, exact_select

CASES = {
    "BOOLEAN": ["true", "false"],
    "TINYINT": ["-128", "127"],
    "SMALLINT": ["-32768", "32767"],
    "INTEGER": ["-2147483648", "2147483647"],
    "UTINYINT": ["0", "255"],
    "USMALLINT": ["65535"],
    "UINTEGER": ["4294967295"],
    "BIGINT": ["-9223372036854775808", "9223372036854775807"],
    "UBIGINT": ["18446744073709551615"],
    "HUGEINT": [
        "-170141183460469231731687303715884105728",
        "170141183460469231731687303715884105727",
    ],
    "UHUGEINT": ["340282366920938463463374607431768211455"],
    "DECIMAL(38,10)": ["9999999999999999999999999999.9999999999", "-0.0000000001"],
    "DECIMAL(12,2)": ["-0.00", "12.50"],
    "DOUBLE": ["1.5", "NaN", "Infinity", "-Infinity"],
    "FLOAT": ["0.1", "-3.4028235e38", "NaN"],
    "VARCHAR": ["", 'ñandú "quoted"'],
    "DATE": ["1850-01-01", "2150-12-31", "-0044-03-15", "29349-01-26", "infinity", "-infinity"],
    "TIMESTAMP": ["1850-01-01 00:00:00", "2150-06-01 12:00:00.123456", "infinity", "-infinity"],
    "TIMESTAMPTZ": [
        "1850-01-01 00:00:00+00",
        "2150-06-01 08:00:00.5-04",
        "40000-01-01 00:00:00+00",
        "infinity",
        "-infinity",
    ],
    "INTERVAL": ["1 year 2 months 3 days 04:05:06.789", "-1 day"],
    "UUID": ["2f1e0b8e-7a4d-4b8e-9c1a-0d2b3c4d5e6f"],
    "BLOB": ["\\x00\\xFFab"],
}


def encode(sql: str) -> dict[str, object]:
    connection = duckdb.connect()
    try:
        connection.sql("SET TimeZone = 'UTC'")
        relation = connection.sql(sql)
        types = [str(kind) for kind in relation.types]
        exact = exact_select(sql, list(relation.columns), types)
        if exact is not None:
            relation = connection.sql(exact)
        source = relation.to_arrow_reader(1000)
        schema = _with_duckdb_types(source.schema, types)
        reader = pa.RecordBatchReader.from_batches(schema, (b.cast(schema) for b in source))
        text = "".join(arrow_to_json(reader))
    finally:
        connection.close()
    document: dict[str, object] = json.loads(text)
    ResultJson.model_validate(document)
    return document


def cast_back(value: object, kind: str) -> str:
    """The original literal re-read from its JSON value, normalized by DuckDB."""
    connection = duckdb.connect()
    try:
        connection.sql("SET TimeZone = 'UTC'")
        if kind == "INTERVAL":
            assert isinstance(value, str)
            months, rest = value[1:].split("M", 1)
            days, seconds = rest.split("DT", 1)
            literal = f"{months} months {days} days {seconds.rstrip('S')} seconds"
            row = connection.execute(
                "SELECT CAST(CAST($v AS INTERVAL) AS VARCHAR)", {"v": literal}
            ).fetchone()
        elif kind == "BLOB":
            row = connection.execute(
                "SELECT CAST(from_base64($v) AS VARCHAR)", {"v": value}
            ).fetchone()
        else:
            text = json.dumps(value) if isinstance(value, bool) else str(value)
            row = connection.execute(
                f"SELECT CAST(CAST($v AS {kind}) AS VARCHAR)", {"v": text}
            ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def original(literal: str, kind: str) -> str:
    connection = duckdb.connect()
    try:
        connection.sql("SET TimeZone = 'UTC'")
        row = connection.execute(
            f"SELECT CAST(CAST($v AS {kind}) AS VARCHAR)", {"v": literal}
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


@pytest.mark.parametrize(("kind", "literals"), CASES.items())
def test_round_trip(kind: str, literals: list[str]) -> None:
    values = " UNION ALL ".join(f"SELECT CAST('{literal}' AS {kind}) AS v" for literal in literals)
    document = encode(f"SELECT * FROM ({values} UNION ALL SELECT NULL) ORDER BY v NULLS LAST")
    rows = document["rows"]
    assert isinstance(rows, list)
    assert rows[-1] == [None]
    encoded = [row[0] for row in rows[:-1]]
    expected = sorted(original(literal, kind) for literal in literals)
    assert sorted(cast_back(value, kind) for value in encoded) == expected


def test_exact_shapes() -> None:
    document = encode(
        "SELECT 12.5::DECIMAL(12,2) AS d, -0.00::DECIMAL(12,2) AS z, "
        "18446744073709551615::UBIGINT AS u, 0::INTEGER AS i, '' AS s, NULL::INTEGER AS n, "
        "TIMESTAMPTZ '2026-10-27 08:30:00-04' AS t, 'NaN'::DOUBLE AS nan"
    )
    assert document["rows"] == [
        ["12.50", "0.00", "18446744073709551615", 0, "", None, "2026-10-27T12:30:00.000000Z", "NaN"]
    ]
    assert document["columns"][0] == {"name": "d", "type": "DECIMAL(12,2)"}  # type: ignore[index]


def test_text_shapes_are_exact() -> None:
    document = encode(
        "SELECT 0.0000001::DECIMAL(18,10) AS a, 0::DECIMAL(38,10) AS b, -0.00::DECIMAL(5,2) AS c, "
        "0.1::FLOAT AS f, DATE '-0044-03-15' AS bc, 'infinity'::DATE AS forever, "
        "TIMESTAMPTZ '40000-01-01 00:00:00+00' AS far, TIMESTAMP '1850-01-01 00:00:00' AS old"
    )
    assert document["rows"] == [
        [
            "0.0000001000",
            "0.0000000000",
            "0.00",
            0.1,
            "-0044-03-15",
            "infinity",
            "40000-01-01T00:00:00.000000Z",
            "1850-01-01T00:00:00.000000",
        ]
    ]


def test_duplicate_column_names_keep_their_values() -> None:
    document = encode(
        "SELECT 1::INTEGER AS x, 340282366920938463463374607431768211455::UHUGEINT AS x"
    )
    assert document["columns"] == [
        {"name": "x", "type": "INTEGER"},
        {"name": "x", "type": "UHUGEINT"},
    ]
    assert document["rows"] == [[1, "340282366920938463463374607431768211455"]]


def test_wide_values_never_become_floats() -> None:
    document = encode(
        "SELECT 9007199254740993::BIGINT AS b, "
        "0.123456789012345678901234567890::DECIMAL(38,30) AS d"
    )
    assert document["rows"] == [["9007199254740993", "0.123456789012345678901234567890"]]


@pytest.mark.parametrize(
    "sql", ["SELECT [1, 2] AS l", "SELECT {'a': 1} AS s", "SELECT TIME '10:00' AS t"]
)
def test_unsupported_types_fail_before_rows(sql: str) -> None:
    with pytest.raises(StoreError) as raised:
        encode(sql)
    assert raised.value.code == "STORE_TYPE_UNSUPPORTED"
    assert raised.value.details["column"] in {"l", "s", "t"}
    assert raised.value.details["type"]


def test_unsupported_type_fails_on_the_first_fragment() -> None:
    connection = duckdb.connect()
    try:
        relation = connection.sql("SELECT 1 AS ok, [1] AS l")
        source = relation.to_arrow_reader(10)
        schema = _with_duckdb_types(source.schema, [str(kind) for kind in relation.types])
        fragments = arrow_to_json(pa.RecordBatchReader.from_batches(schema, iter(())))
        with pytest.raises(StoreError):
            next(fragments)
    finally:
        connection.close()


@settings(max_examples=200, deadline=None)
@given(
    value=st.decimals(
        min_value=decimal.Decimal("-9999999999999999999999999999.9999999999"),
        max_value=decimal.Decimal("9999999999999999999999999999.9999999999"),
        places=10,
        allow_nan=False,
        allow_infinity=False,
    ),
    whole=st.integers(min_value=-(2**127), max_value=2**127 - 1),
)
def test_property_decimals_and_hugeints(value: decimal.Decimal, whole: int) -> None:
    text = format(value, "f")
    document = encode(
        f"SELECT CAST('{text}' AS DECIMAL(38,10)) AS d, CAST('{whole}' AS HUGEINT) AS h"
    )
    decoded, huge = document["rows"][0]  # type: ignore[index]
    assert decimal.Decimal(decoded) == value
    with decimal.localcontext(prec=60):
        scaled = value.quantize(decimal.Decimal("1E-10"))
    assert decoded == format(scaled.copy_abs() if scaled.is_zero() else scaled, "f")
    assert int(huge) == whole
