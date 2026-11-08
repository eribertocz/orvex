"""Exactness of file-source views, over every combination of types (#252, law 11).

Differential: for each Parquet type alone, and each pair of types in two files read together,
the view either gives back exactly the values written or refuses the column with
UNSUPPORTED_SOURCE_TYPE; the other columns stay readable. CSV texts, folder names
(`name=value`), names that differ only in case, and files rewritten after the view was made are
covered the same way. Plain DuckDB, no lake: these run everywhere.
"""

from __future__ import annotations

import datetime as dt
import decimal
import itertools
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from orvex.store.errors import StoreError
from orvex.store.file_sources import view_query

from tests.support import parquet_footer
from tests.support.fixtures import frozen

D = decimal.Decimal
UTC = dt.UTC
WIDE = D("123456789012345678901234567890123456.78")

# Each type with values at its edges; every value must come back unchanged or be refused.
TYPES: dict[str, tuple[pa.DataType, list[object]]] = {
    "int8": (pa.int8(), [-128, 127]),
    "int16": (pa.int16(), [-32768, 32767]),
    "int32": (pa.int32(), [-(2**31), 2**31 - 1]),
    "int64": (pa.int64(), [-(2**63), 2**63 - 1, 2**53 + 1]),
    "uint8": (pa.uint8(), [255]),
    "uint16": (pa.uint16(), [65535]),
    "uint32": (pa.uint32(), [2**32 - 1]),
    "uint64": (pa.uint64(), [2**64 - 1]),
    "float32": (pa.float32(), [1.100000023841858, 3.4028234663852886e38]),
    "float64": (pa.float64(), [0.1, 2.0**60, 1e308]),
    "d10_1": (pa.decimal128(10, 1), [D("123456789.1")]),
    "d18_4": (pa.decimal128(18, 4), [D("12345678901234.5678")]),
    "d38_0": (pa.decimal128(38, 0), [D("9" * 38)]),
    "d38_2": (pa.decimal128(38, 2), [WIDE]),
    "d38_10": (pa.decimal128(38, 10), [D("1234567890123456789012345678.1234567890")]),
    "d45_2": (pa.decimal256(45, 2), [D("1234567890123456789012345678901234567890123.45")]),
    "bool": (pa.bool_(), [True]),
    "string": (pa.string(), ["1.50"]),
    "large_string": (pa.large_string(), ["abc"]),
    "binary": (pa.binary(), [b"\x00\xff"]),
    "date32": (pa.date32(), [dt.date(2026, 1, 31)]),
    "date64": (pa.date64(), [dt.date(1900, 1, 1)]),
    "ts_s": (pa.timestamp("s"), [dt.datetime(2026, 1, 1, 10, 0, 1)]),
    "ts_ms": (pa.timestamp("ms"), [dt.datetime(2026, 1, 1, 10, 0, 0, 123000)]),
    "ts_us": (pa.timestamp("us"), [dt.datetime(2026, 1, 1, 10, 0, 0, 123456)]),
    "ts_ns": (pa.timestamp("ns"), [1767261600123456789]),
    "ts_us_utc": (pa.timestamp("us", tz="UTC"), [dt.datetime(2026, 1, 1, 10, tzinfo=UTC)]),
    "time64": (pa.time64("us"), [dt.time(23, 59, 59, 999999)]),
    "time32": (pa.time32("ms"), [dt.time(1, 2, 3, 4000)]),
    "struct_int": (pa.struct([("a", pa.int64())]), [{"a": 2**62}]),
    "struct_d10": (pa.struct([("a", pa.decimal128(38, 10))]), [{"a": D("1.1234567890")}]),
    "struct_d2": (pa.struct([("a", pa.decimal128(38, 2))]), [{"a": WIDE}]),
    "list_int": (pa.list_(pa.int64()), [[1, 2**62]]),
    "list_d10": (pa.list_(pa.decimal128(38, 10)), [[D("1.1234567890")]]),
    "list_d2": (pa.list_(pa.decimal128(38, 2)), [[WIDE]]),
    "dictionary": (pa.dictionary(pa.int32(), pa.string()), ["x"]),
}
MISSING = "missing"  # a file without the column


class Written(NamedTuple):
    path: Path
    values: dict[int, object]  # by row key, as written
    texts: dict[int, str]  # by row key, DuckDB's text of the value read alone


def _write(folder: Path, name: str, start: int, side: str) -> tuple[Path, dict[int, object]]:
    """A file with column `k` (row keys from `start`) and `m` of type `name`; the values
    written, by key, as plain Python values (timestamps as epoch nanoseconds)."""
    path = folder / f"{side}_{name}.parquet"
    if name == MISSING:
        pq.write_table(pa.table({"k": pa.array([start], pa.int64())}), path)
        return path, {start: None}
    kind, values = TYPES[name]
    keys = pa.array([start + index for index in range(len(values))], pa.int64())
    pq.write_table(pa.table({"k": keys, "m": pa.array(values, kind)}), path)
    column = pq.read_table(path).column("m")
    if pa.types.is_timestamp(column.type) or pa.types.is_date(column.type):
        target = pa.timestamp("ns", tz=getattr(column.type, "tz", None))
        column = column.cast(target).cast(pa.int64())
    return path, dict(zip(keys.to_pylist(), column.to_pylist(), strict=True))


def _view(connection: duckdb.DuckDBPyConnection, kind: str, files: list[Path]) -> None:
    query = view_query(connection, kind, "src", [str(file) for file in files])
    connection.execute(f"CREATE OR REPLACE VIEW data AS {query}")


def _plain(value: object) -> object:
    """A value as comparable Python: numbers as Decimal, nested values element by element."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float | D):
        return D(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    return value


def _outcome(
    connection: duckdb.DuckDBPyConnection, expected: dict[int, object], texts: dict[int, str]
) -> str:
    """`exact` or `refused`; any other outcome is described, so the assertion shows it.
    `texts` is DuckDB's text of each value read alone: a value united as text must keep it."""
    keys = connection.execute("SELECT list(k ORDER BY k) FROM data").fetchone()
    if keys is None or sorted(keys[0]) != sorted(expected):
        return f"column k changed: {keys}"
    try:
        kind_row = connection.execute("SELECT any_value(typeof(m)) FROM data").fetchone()
        kind = str(kind_row[0]) if kind_row else ""
        value = "epoch_ns(m)" if kind.startswith("TIMESTAMP") else "m"
        if kind == "DATE":
            value = "epoch_ns(CAST(m AS TIMESTAMP))"
        row = connection.execute(
            f"SELECT list({{'k': k, 'v': {value}}} ORDER BY k) FROM data"
        ).fetchone()
    except duckdb.Error as error:
        return "refused" if "UNSUPPORTED_SOURCE_TYPE" in str(error) else f"error: {error}"
    got = {int(item["k"]): item["v"] for item in (row[0] if row else [])}
    changed: dict[int, tuple[object, object]]
    if kind == "VARCHAR":
        changed = {k: (texts.get(k), v) for k, v in got.items() if v != texts.get(k)}
    else:
        changed = {
            k: (w, got.get(k)) for k, w in expected.items() if _plain(w) != _plain(got.get(k))
        }
    return "exact" if not changed else f"changed as {kind}: {changed}"


def _texts(connection: duckdb.DuckDBPyConnection, file: Path) -> dict[int, str]:
    """DuckDB's text of every value of `m` in `file` read alone, by key."""
    try:
        row = connection.execute(
            f"SELECT list({{'k': k, 'v': CAST(m AS VARCHAR)}}) FROM read_parquet('{file}')"
        ).fetchone()
    except duckdb.BinderException:
        return {}  # the file has no column m
    return {int(item["k"]): item["v"] for item in (row[0] if row else [])}


@pytest.fixture(scope="module")
def parquet_files(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, list[Written]]]:
    """Two files per type (sides a and b), with distinct row keys, read-only for every test."""
    folder = tmp_path_factory.mktemp("types")
    connection = duckdb.connect()
    files: dict[str, list[Written]] = {}
    for index, name in enumerate([*TYPES, MISSING]):
        files[name] = []
        for side, start in (("a", index * 1000), ("b", index * 1000 + 500)):
            path, values = _write(folder, name, start, side)
            files[name].append(Written(path, values, _texts(connection, path)))
    connection.close()
    for _ in frozen(folder):
        yield files


def test_every_parquet_type_alone_is_exact_except_a_wide_decimal(
    parquet_files: dict[str, list[Written]],
) -> None:
    connection = duckdb.connect()
    outcomes = {}
    for name in TYPES:
        first = parquet_files[name][0]
        _view(connection, "parquet", [first.path])
        outcomes[name] = _outcome(connection, first.values, first.texts)
    assert {name for name, got in outcomes.items() if got != "exact"} == {"d45_2"}, outcomes
    assert outcomes["d45_2"] == "refused"


def test_every_pair_of_parquet_types_in_two_files_is_exact_or_refused(
    parquet_files: dict[str, list[Written]],
) -> None:
    connection = duckdb.connect()
    wrong = {}
    for left, right in itertools.product([*TYPES, MISSING], repeat=2):
        if left == right == MISSING:
            continue
        first, second = parquet_files[left][0], parquet_files[right][1]
        _view(connection, "parquet", [first.path, second.path])
        values, texts = {**first.values, **second.values}, {**first.texts, **second.texts}
        got = _outcome(connection, values, texts)
        if got not in ("exact", "refused"):
            wrong[(left, right)] = got
        if left == right and left != "d45_2" and got != "exact":
            wrong[(left, right)] = f"the same type twice must be exact: {got}"
    assert not wrong, wrong


@pytest.mark.parametrize(
    ("upper", "lower"),
    [
        (pa.decimal256(50, 2), [pa.decimal256(45, 2)]),
        (pa.decimal128(10, 2), [pa.decimal128(38, 2), pa.decimal128(38, 10)]),
    ],
)
def test_names_differing_only_in_a_non_ascii_letter_case_are_one_column(
    tmp_path: Path, upper: pa.DataType, lower: list[pa.DataType]
) -> None:
    # DuckDB matches names ignoring ASCII case only: MONTO_AÑO and monto_año are two columns,
    # and the files giving monto_año unite lossily.
    files = [tmp_path / "0.parquet"]
    pq.write_table(pa.table({"MONTO_AÑO": pa.array([D("2.25")], upper), "k": [0]}), files[0])
    for index, kind in enumerate(lower, start=1):
        files.append(tmp_path / f"{index}.parquet")
        value = D("1.1234567890").quantize(D(1).scaleb(-kind.scale))
        pq.write_table(pa.table({"monto_año": pa.array([value], kind), "k": [index]}), files[-1])
    connection = duckdb.connect()
    _view(connection, "parquet", files)
    assert [row[0] for row in connection.execute("DESCRIBE data").fetchall()] == [
        "MONTO_AÑO",
        "k",
        "monto_año",
    ]
    with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
        connection.execute('SELECT "monto_año" FROM data').fetchall()
    assert connection.execute("SELECT count(k) FROM data").fetchall() == [(len(files),)]


def test_a_file_column_also_named_by_its_folder_is_refused_when_the_values_differ(
    tmp_path: Path,
) -> None:
    for kind, write in (("parquet", _parquet_file), ("csv", _csv_file)):
        differ = tmp_path / kind / "amount=9.99" / f"f.{kind}"
        write(differ, {"amount": D("1.10"), "k": 1})
        agree = tmp_path / kind / "year=2026" / f"f.{kind}"
        write(agree, {"year": 2026, "k": 2})
        only = tmp_path / kind / "code=007" / f"f.{kind}"
        write(only, {"k": 3})
        connection = duckdb.connect()
        _view(connection, kind, [differ])
        with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
            connection.execute("SELECT amount FROM data").fetchall()
        assert connection.execute("SELECT CAST(k AS VARCHAR) FROM data").fetchall() == [("1",)]
        _view(connection, kind, [agree])
        assert connection.execute("SELECT CAST(year AS VARCHAR) FROM data").fetchall() == [
            ("2026",)
        ]
        _view(connection, kind, [only])
        assert connection.execute("SELECT code FROM data").fetchall() == [("007",)]


def _parquet_file(path: Path, values: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({name: [value] for name, value in values.items()}), path)


def _csv_file(path: Path, values: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(values) + "\n" + ",".join(str(v) for v in values.values()) + "\n")


# CSV texts: each comes back as the same text, or typed by a type that gives the text back.
CSV_TEXTS = [
    "7",
    "-42",
    "007",
    "+5",
    "0x10",
    "0b101",
    "1e3",
    "1.5",
    "0.1",
    "92233720368547758070",
    "true",
    "TRUE",
    "t",
    "2026-01-31",
    "01/02/2026",
    "13/02/2026",
    "2026-01-31 10:00:00",
    "2026-01-31T10:00:00",
    "2026-01-31 10:00:00.1",
    "2026-01-31 10:00:00.1234567",
    "2026-01-31 10:00:00+02",
    "2026-01-31 10:00:00.123456+02:00",
    "2026-01-31 10:00:00Z",
    "2026-01-31 10:00:00+25",
    "2026-01-31 10:00:00+05:99",
    "abc",
]


def _csv_changes(connection: duckdb.DuckDBPyConnection, texts: list[str]) -> list[object]:
    """The texts (row k holds texts[k]) the view does not give back unchanged."""
    kind_row = connection.execute("SELECT any_value(typeof(v)) FROM data").fetchone()
    kind = str(kind_row[0]) if kind_row else ""
    if kind == "TIMESTAMP WITH TIME ZONE":
        same = "v = CAST(t AS TIMESTAMPTZ) AND regexp_matches(t, '(Z|[+-]\\d\\d(:?\\d\\d)?)$')"
    elif kind == "BOOLEAN":
        same = "CAST(v AS VARCHAR) = lower(t)"
    else:
        same = "CAST(v AS VARCHAR) = t"
    row = connection.execute(
        f"SELECT list([t, CAST(v AS VARCHAR), {_literal(kind)}]) FILTER (WHERE NOT ({same})) "
        "FROM data JOIN (SELECT unnest($texts) AS t, generate_subscripts($texts, 1) AS k) "
        "USING (k)",
        {"texts": texts},
    ).fetchone()
    return list(row[0] or []) if row else []


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def test_every_csv_text_alone_and_in_pairs_comes_back_unchanged(tmp_path: Path) -> None:
    connection = duckdb.connect()
    changed = {}
    for left, right in itertools.product(CSV_TEXTS, repeat=2):
        first, second = tmp_path / "a.csv", tmp_path / "b.csv"
        first.write_text(f"k,v\n1,{left}\n")
        second.write_text(f"k,v\n2,{right}\n")
        for files, texts in (([first], [left]), ([first, second], [left, right])):
            _view(connection, "csv", files)
            bad = _csv_changes(connection, texts)
            if bad:
                changed[tuple(texts)] = bad
    assert not changed, changed


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("7", "BIGINT"),
        ("true", "BOOLEAN"),
        ("2026-01-31", "DATE"),
        ("2026-01-31 10:00:00", "TIMESTAMP"),
        ("2026-01-31 10:00:00-04", "TIMESTAMP WITH TIME ZONE"),
        ("0.1", "VARCHAR"),
    ],
)
def test_a_csv_text_that_survives_its_detected_type_keeps_it(
    tmp_path: Path, text: str, kind: str
) -> None:
    (tmp_path / "f.csv").write_text(f"v\n{text}\n")
    connection = duckdb.connect()
    _view(connection, "csv", [tmp_path / "f.csv"])
    assert connection.execute("SELECT typeof(v) FROM data").fetchall() == [(kind,)]


def test_a_timestamp_without_offset_never_takes_the_session_time_zone(tmp_path: Path) -> None:
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    first.write_text("v\n2026-01-31 10:00:00+02\n")
    second.write_text("v\n2026-01-31 10:00:00\n")
    connection = duckdb.connect()
    _view(connection, "csv", [first, second])
    assert connection.execute("SELECT list(v ORDER BY v) FROM data").fetchall() == [
        (["2026-01-31 10:00:00", "2026-01-31 10:00:00+02"],)
    ]


def test_a_file_rewritten_after_the_view_was_made_fails_instead_of_changing(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "f.parquet"
    pq.write_table(pa.table({"m": pa.array([D("1.10")], pa.decimal128(10, 2)), "k": [1]}), parquet)
    connection = duckdb.connect()
    _view(connection, "parquet", [parquet])
    pq.write_table(pa.table({"m": pa.array([D("1.10")], pa.decimal256(50, 2)), "k": [1]}), parquet)
    for column in ("m", "k"):  # the schema changed: nothing is served until attached again
        with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
            connection.execute(f"SELECT {column} FROM data").fetchall()
    csv = tmp_path / "f.csv"
    csv.write_text("n\n7\n")
    _view(connection, "csv", [csv])
    assert connection.execute("SELECT n, typeof(n) FROM data").fetchall() == [(7, "BIGINT")]
    csv.write_text("n\n007\n")
    with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
        connection.execute("SELECT n FROM data").fetchall()


@pytest.mark.parametrize("rewritten", sorted(set(TYPES) - {"d10_1"}))
def test_any_file_of_several_rewritten_with_another_type_fails_every_read(
    tmp_path: Path, rewritten: str
) -> None:
    # Each file is checked, not only the type the files unite in: DECIMAL(38,2) beside
    # DECIMAL(10,2) unites as DECIMAL(38,2), and so it does after the second file becomes
    # DECIMAL(38,10), which would round its values.
    first, _ = _write(tmp_path, "d38_2", 0, "a")
    second, _ = _write(tmp_path, "d10_1", 500, "b")
    connection = duckdb.connect()
    _view(connection, "parquet", [first, second])
    assert connection.execute("SELECT count(m) FROM data").fetchall() == [(2,)]
    _write(tmp_path, rewritten, 500, "b")
    (tmp_path / f"b_{rewritten}.parquet").replace(second)
    with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
        connection.execute("SELECT k FROM data").fetchall()


# Folder values, raw as in the path: DuckDB decodes %XX and may type them (`year=2026`).
FOLDER_TEXTS = [
    *(text for text in CSV_TEXTS if "/" not in text),
    "a%20b",
    "San%20Jos%C3%A9",
    "a%2Fb",
    " 5",
    "-0",
]


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_every_folder_value_comes_back_as_duckdb_decodes_it_or_typed_exactly(
    tmp_path: Path, kind: str
) -> None:
    connection = duckdb.connect()
    changed = {}
    for index, raw in enumerate(FOLDER_TEXTS):
        file = tmp_path / str(index) / f"x={raw}" / f"f.{kind}"
        (_parquet_file if kind == "parquet" else _csv_file)(file, {"k": index})
        _view(connection, kind, [file])
        text = urllib.parse.unquote(raw)
        row = connection.execute(
            "SELECT typeof(x), CAST(x AS VARCHAR), "
            "CASE WHEN typeof(x) = 'TIMESTAMP WITH TIME ZONE' THEN x = CAST($t AS TIMESTAMPTZ) "
            "ELSE CAST(x AS VARCHAR) = $t END FROM data",
            {"t": text},
        ).fetchone()
        if row is None or row[2] is not True:
            changed[raw] = row
    assert not changed, changed


def test_a_folder_value_is_compared_as_duckdb_decodes_it(tmp_path: Path) -> None:
    # Folder `name=a%20b` is served as 'a b': a file holding 'a%20b' would change silently.
    encoded = tmp_path / "1" / "name=a%20b" / "f.parquet"
    _parquet_file(encoded, {"name": "a%20b", "k": 1})
    accented = tmp_path / "2" / "city=San%20Jos%C3%A9" / "f.parquet"
    _parquet_file(accented, {"city": "San José", "k": 2})
    connection = duckdb.connect()
    _view(connection, "parquet", [encoded])
    with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
        connection.execute("SELECT name FROM data").fetchall()
    _view(connection, "parquet", [accented])
    assert connection.execute("SELECT city FROM data").fetchall() == [("San José",)]


def test_a_csv_view_does_not_read_the_whole_file_again_on_every_query(tmp_path: Path) -> None:
    # Detection reads every row once, when the source is attached; the view's reader keeps
    # DuckDB's default sniffing, so `count(*)` or `LIMIT 5` do not scan the file twice.
    (tmp_path / "f.csv").write_text("v\n7\n")
    query = view_query(duckdb.connect(), "csv", "src", [str(tmp_path / "f.csv")])
    assert "sample_size" not in query


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (["x", "x"], None),
        (["A", "a"], None),
        ([""], None),
        (["x", "x"], ["x_1"]),
        (["A", "a"], ["a_1"]),
    ],
    ids=["duplicate", "case-only", "empty", "renamed-like-another-file", "case-like-another"],
)
def test_a_file_with_columns_duckdb_would_rename_is_refused(
    tmp_path: Path, first: list[str], second: list[str] | None
) -> None:
    # DuckDB renames duplicate, case-colliding or empty names (x_1, a_1, C0), and a made-up
    # name can be a real column of another file: such a source is refused when attached.
    values = [pa.array([1], pa.int64()), pa.array([D("1.1234567890")], pa.decimal128(38, 10))]
    paths = [tmp_path / "a.parquet"]
    pq.write_table(pa.table(values[-len(first) :], names=first), paths[0])
    if second is not None:
        paths.append(tmp_path / "b.parquet")
        pq.write_table(
            pa.table([pa.array([D("1.25")], pa.decimal128(38, 2))], names=second), paths[1]
        )
    with pytest.raises(StoreError) as raised:
        _view(duckdb.connect(), "parquet", paths)
    assert raised.value.code == "STORE_SOURCE_INVALID"


def test_a_csv_column_added_after_attach_with_a_folder_name_fails(tmp_path: Path) -> None:
    file = tmp_path / "year=2026" / "a.csv"
    file.parent.mkdir()
    file.write_text("v\n1\n")
    connection = duckdb.connect()
    _view(connection, "csv", [file])
    assert connection.execute(
        "SELECT CAST(v AS VARCHAR), CAST(year AS VARCHAR) FROM data"
    ).fetchall() == [("1", "2026")]
    file.write_text("v,year\n1,2025\n")  # the file now has its own `year`, which DuckDB hides
    with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
        connection.execute("SELECT v FROM data").fetchall()


def test_an_int96_timestamp_is_refused_at_any_depth(tmp_path: Path) -> None:
    # DuckDB reads INT96 to the microsecond: 1767261600123456789 ns would lose its 789.
    nanos = pa.array([1767261600123456789], pa.timestamp("ns"))
    table = pa.table(
        {"t": nanos, "s": pa.StructArray.from_arrays([nanos], ["t"]), "k": pa.array([1])}
    )
    path = tmp_path / "f.parquet"
    pq.write_table(table, path, use_deprecated_int96_timestamps=True)
    connection = duckdb.connect()
    _view(connection, "parquet", [path])
    for column in ("t", "s"):
        with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
            connection.execute(f"SELECT {column} FROM data").fetchall()
    assert connection.execute("SELECT k FROM data").fetchall() == [(1,)]


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_a_folder_named_null_beside_typed_siblings_stays_text(tmp_path: Path, kind: str) -> None:
    # DuckDB's autocast serves `k=NULL` as SQL NULL beside `k=5`; its decoded text is 'NULL'.
    files = []
    for value, key in (("NULL", 1), ("5", 2), ("6", 3)):
        files.append(tmp_path / f"k={value}" / f"f.{kind}")
        (_parquet_file if kind == "parquet" else _csv_file)(files[-1], {"x": key})
    connection = duckdb.connect()
    _view(connection, kind, files)
    assert connection.execute(
        "SELECT list(CAST(k AS VARCHAR) ORDER BY CAST(x AS VARCHAR)), any_value(typeof(k)) "
        "FROM data"
    ).fetchall() == [(["NULL", "5", "6"], "VARCHAR")]


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_a_file_column_shared_with_its_folder_is_checked_on_every_read(
    tmp_path: Path, kind: str
) -> None:
    file = tmp_path / "year=2026" / f"f.{kind}"
    write = _parquet_file if kind == "parquet" else _csv_file
    write(file, {"year": 2026, "v": 1})
    connection = duckdb.connect()
    _view(connection, kind, [file])
    assert connection.execute("SELECT CAST(year AS VARCHAR) FROM data").fetchall() == [("2026",)]
    write(file, {"year": 2025, "v": 1})  # the file now disagrees with its folder
    with pytest.raises(duckdb.Error, match="UNSUPPORTED_SOURCE_TYPE"):
        connection.execute("SELECT year, v FROM data").fetchall()


def test_an_all_null_column_beside_a_typed_one_reads_exactly(tmp_path: Path) -> None:
    # pyarrow and pandas write a column with only nulls as Parquet's null type.
    first, second = tmp_path / "a.parquet", tmp_path / "b.parquet"
    pq.write_table(pa.table({"m": pa.array([None], pa.null()), "k": [1]}), first)
    pq.write_table(pa.table({"m": pa.array([D("1.25")], pa.decimal128(10, 2)), "k": [2]}), second)
    connection = duckdb.connect()
    _view(connection, "parquet", [first, second])
    assert connection.execute(
        "SELECT list(CAST(m AS VARCHAR) ORDER BY k) FROM data"
    ).fetchall() == [([None, "1.25"],)]


@pytest.mark.parametrize(
    "files",
    [
        ["1.5,2.5\n3.5,4.5\n"],
        ["A1,1.5\nB2,2.5\n"],
        ["1\n" * 30000 + "x\n"],
        ["a,b\n1.5,2.5\n", "1.5,2.5\n"],
    ],
    ids=["decimals", "text-and-decimals", "late-text", "beside-a-header"],
)
def test_a_headerless_csv_is_read_with_one_header_decision(
    tmp_path: Path, files: list[str]
) -> None:
    # The view's reader decides the header once; types are checked on that same reader, so a
    # headerless file of decimals is attached and its values arrive as their exact text.
    paths = []
    for index, text in enumerate(files):
        paths.append(tmp_path / f"{index}.csv")
        paths[-1].write_text(text)
    connection = duckdb.connect()
    _view(connection, "csv", paths)
    reader = ", ".join(f"'{path}'" for path in paths)
    expected = connection.execute(
        f"SELECT * FROM read_csv([{reader}], union_by_name = true, all_varchar = true)"
    ).fetchall()
    served = connection.execute("SELECT * FROM data").fetchall()
    assert [tuple(None if v is None else str(v) for v in row) for row in served] == expected


# --- #260: a decimal with only the logical annotation ---------------------------------------------


def _logical_only(tmp_path: Path, integers: bool) -> Path:
    cents = [decimal.Decimal("1.10"), None]
    table = pa.table(
        {
            "small": pa.array(cents, pa.decimal128(9, 2)),
            "wide": pa.array(cents, pa.decimal128(18, 2)),
            "nested": pa.StructArray.from_arrays([pa.array(cents, pa.decimal128(10, 2))], ["v"]),
            "listed": pa.array([[cents[0]], []], pa.list_(pa.decimal128(12, 2))),
            "k": pa.array([1, 2]),
        }
    )
    path = tmp_path / "f.parquet"
    pq.write_table(table, path, store_decimal_as_integer=integers)
    parquet_footer.without_converted_types(path)
    return path


@pytest.mark.parametrize("integers", [False, True], ids=["fixed-bytes", "int32-int64"])
def test_a_decimal_duckdb_does_not_read_as_one_is_refused_at_any_depth(
    tmp_path: Path, integers: bool
) -> None:
    # DuckDB v2.0.0 reads a logical-only decimal as BLOB on fixed bytes, and as the unscaled
    # integer on INT32/INT64 (1.10 as 110): never a value a model may read.
    path = _logical_only(tmp_path, integers)
    connection = duckdb.connect()
    _view(connection, "parquet", [path])
    for column in ("small", "wide", "nested", "listed"):
        with pytest.raises(duckdb.Error, match=f"UNSUPPORTED_SOURCE_TYPE: column {column} "):
            connection.execute(f"SELECT {column} FROM data").fetchall()
    assert connection.execute("SELECT k FROM data ORDER BY k").fetchall() == [(1,), (2,)]


def test_the_rewritten_footer_keeps_every_value_and_drops_only_converted_annotations(
    tmp_path: Path,
) -> None:
    path = _logical_only(tmp_path, integers=False)
    read = pq.read_table(path).to_pylist()
    assert read[0]["small"] == decimal.Decimal("1.10")
    assert read[0]["nested"] == {"v": decimal.Decimal("1.10")}
    rows = (
        duckdb.connect()
        .execute(f"SELECT converted_type, logical_type FROM parquet_schema('{path}')")
        .fetchall()
    )
    assert sum(1 for _, logical in rows if logical and logical.startswith("DecimalType")) == 4
    assert all(converted is None for converted, logical in rows if logical)


@pytest.mark.parametrize("integers", [False, True])
def test_the_footer_codec_gives_back_an_unchanged_footer_byte_for_byte(
    tmp_path: Path, integers: bool
) -> None:
    path = tmp_path / "f.parquet"
    pq.write_table(_every_kind(), path, store_decimal_as_integer=integers)
    data = path.read_bytes()
    size = int.from_bytes(data[-8:-4], "little")
    footer = data[-8 - size : -8]
    writer = parquet_footer._Writer()
    writer.struct(parquet_footer._Reader(footer).struct())
    assert bytes(writer.out) == footer


def _every_kind() -> pa.Table:
    return pa.table(
        {
            "u8": pa.array([255], pa.uint8()),
            "u64": pa.array([2**64 - 1], pa.uint64()),
            "i16": pa.array([-1], pa.int16()),
            "date": pa.array([dt.date(2026, 1, 2)]),
            "text": pa.array(["a"]),
            "items": pa.array([[1]], pa.list_(pa.int64())),
            "pairs": pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int64())),
            "money": pa.array([decimal.Decimal("1.10")], pa.decimal128(9, 2)),
            "stamp": pa.array([1], pa.timestamp("us", tz="UTC")),
            "clock": pa.array([1], pa.time64("us")),
            "k": pa.array([7]),
        }
    )


def test_every_logical_only_type_duckdb_misreads_is_refused_and_the_rest_read(
    tmp_path: Path,
) -> None:
    # Measured on DuckDB v2.0.0: without the converted twin, an unsigned is read as signed
    # (2^64-1 as -1), a date as its day number, a string as BLOB, a list or a map as a struct.
    path = tmp_path / "f.parquet"
    pq.write_table(_every_kind(), path)
    parquet_footer.without_converted_types(path)
    connection = duckdb.connect()
    _view(connection, "parquet", [path])
    for column in ("u8", "u64", "i16", "date", "text", "items", "pairs", "money"):
        with pytest.raises(duckdb.Error, match=f"UNSUPPORTED_SOURCE_TYPE: column {column} "):
            connection.execute(f"SELECT {column} FROM data").fetchall()
    assert connection.execute("SELECT k, epoch_us(stamp), clock FROM data").fetchall() == [
        (7, 1, dt.time(0, 0, 0, 1))
    ]


def test_a_decimal_with_both_annotations_still_reads_exactly(tmp_path: Path) -> None:
    path = tmp_path / "f.parquet"
    table = pa.table({"small": pa.array([decimal.Decimal("1.10")], pa.decimal128(9, 2))})
    pq.write_table(table, path, store_decimal_as_integer=True)
    connection = duckdb.connect()
    _view(connection, "parquet", [path])
    assert connection.execute("SELECT small FROM data").fetchall() == [(decimal.Decimal("1.10"),)]


def test_an_ordinary_file_with_logical_only_types_duckdb_honors_reads(tmp_path: Path) -> None:
    # pyarrow writes these with the logical annotation only: each is read as its own type.
    path = tmp_path / "f.parquet"
    table = pa.table(
        {
            "half": pa.array([1.5], pa.float16()),
            "nothing": pa.array([None], pa.null()),
            "nanos": pa.array([1767261600123456789], pa.timestamp("ns")),
            "id": pa.array([b"0123456789abcdef"], pa.uuid()),
        }
    )
    pq.write_table(table, path)
    connection = duckdb.connect()
    _view(connection, "parquet", [path])
    assert connection.execute(
        "SELECT half, nothing, epoch_ns(nanos), CAST(id AS VARCHAR) FROM data"
    ).fetchall() == [(1.5, None, 1767261600123456789, "30313233-3435-3637-3839-616263646566")]
