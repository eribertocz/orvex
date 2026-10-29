"""#126: the lake's data files in another local folder or in S3-compatible storage, optionally
encrypted.

The S3 tests run against a local S3-compatible server started here (moto, on a free port of
127.0.0.1, with disposable credentials), so plain HTTP is allowed: the endpoint is the local
machine. They need DuckDB's httpfs extension, which CI downloads.
"""

from __future__ import annotations

import datetime as dt
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from moto.server import ThreadedMotoServer
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    ReadRequest,
    S3Storage,
    SqliteLakeLocation,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, _guarded, _sandbox_entries, open_lake

LIMITS = Limits(
    memory_limit="512MB", threads=1, max_concurrent=4, queue_size=16, default_timeout_ms=600_000
)
KEY_ENV, SECRET_ENV = "LAKE_TEST_S3_KEY_ID", "LAKE_TEST_S3_SECRET"
KEY_ID, SECRET = "test-key-id", f"test-secret-{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"{host}:{port}"
    finally:
        server.stop()


# Plain HTTP to the local test server, without any proxy from the environment.
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def s3_request(method: str, url: str, body: bytes | None = None) -> bytes:
    # Without a content type, urllib sends a body as a form, and moto waits on a binary one.
    headers = {"Content-Type": "application/octet-stream"} if body is not None else {}
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    with _HTTP.open(request, timeout=30) as response:
        content: bytes = response.read()
        return content


@pytest.fixture
def bucket(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(KEY_ENV, KEY_ID)
    monkeypatch.setenv(SECRET_ENV, SECRET)
    name = f"lake-{uuid.uuid4().hex[:12]}"
    s3_request("PUT", f"http://{endpoint}/{name}")
    return name


def put(endpoint: str, bucket: str, key: str, body: bytes) -> None:
    s3_request("PUT", f"http://{endpoint}/{bucket}/{key}", body)


def keys(endpoint: str, bucket: str) -> list[str]:
    listed = ElementTree.fromstring(s3_request("GET", f"http://{endpoint}/{bucket}?list-type=2"))
    return [element.text or "" for element in listed.iter() if element.tag.endswith("}Key")]


def storage(endpoint: str, use_ssl: bool = False) -> S3Storage:
    return S3Storage(
        type="s3",
        region="us-east-1",
        endpoint=endpoint,
        url_style="path",
        use_ssl=use_ssl,
        key_id_env=KEY_ENV,
        secret_env=SECRET_ENV,
    )


def s3_lake(tmp_path: Path, endpoint: str, bucket: str, encrypted: bool = False) -> Lake:
    location = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path=f"s3://{bucket}/billing/",
        storage=storage(endpoint),
        encrypted=encrypted,
    )
    return open_lake(location, LIMITS)


def write(lake: Lake, sql: str) -> None:
    with lake.transaction() as transaction:
        transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))


def read(lake: Lake, sql: str) -> list[dict[str, object]]:
    request = ReadRequest(
        kind="read", sql=sql, parameters=[], snapshot=lake.current_snapshot().id, batch_size=1024
    )
    found: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
    return found


READINGS = (
    "CREATE TABLE readings AS SELECT i::BIGINT AS id, (i / 8)::DECIMAL(18,4) AS kwh "
    "FROM range(1, 2001) AS r(i)"
)
TOTAL = "SELECT count(*) AS n, CAST(sum(kwh) AS VARCHAR) AS total FROM readings"
EXPECTED = [{"n": 2000, "total": "250125.0000"}]


# --- a local data folder ---------------------------------------------------------------------


def test_data_files_in_another_local_folder(tmp_path: Path) -> None:
    data = tmp_path / "elsewhere" / "data"
    location = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path=data.as_posix() + "/",
    )
    lake = open_lake(location, LIMITS)
    try:
        write(lake, READINGS)
        assert read(lake, TOTAL) == EXPECTED
    finally:
        lake.close()
    assert list(data.rglob("*.parquet"))
    assert not list((tmp_path / "lake").rglob("*.parquet"))


# --- S3-compatible storage -------------------------------------------------------------------


def test_a_lake_on_object_storage(tmp_path: Path, endpoint: str, bucket: str) -> None:
    lake = s3_lake(tmp_path, endpoint, bucket)
    try:
        write(lake, READINGS)
        assert read(lake, TOTAL) == EXPECTED
        # The secret is temporary, and DuckDB never shows its value.
        secrets = lake._connection.execute(
            "SELECT name, persistent, secret_string FROM duckdb_secrets() WHERE type = 's3'"
        ).fetchall()
        assert [(name, persistent) for name, persistent, _ in secrets] == [("lake_storage", False)]
        assert SECRET not in str(secrets)
        # No secret can be persisted, and none is in DuckDB's secret folder.
        setting = lake._connection.execute(
            "SELECT current_setting('allow_persistent_secrets'), "
            "current_setting('secret_directory')"
        ).fetchone()
        assert setting is not None
        assert setting[0] is False
        directory = Path(str(setting[1]))
        stored = [path for path in directory.rglob("*") if path.is_file()]
        assert not any(SECRET.encode() in path.read_bytes() for path in stored)
    finally:
        lake.close()
    found = keys(endpoint, bucket)
    assert found
    assert all(key.startswith("billing/") for key in found)  # under the declared prefix only
    reopened = s3_lake(tmp_path, endpoint, bucket)
    try:
        assert read(reopened, TOTAL) == EXPECTED
    finally:
        reopened.close()


def test_the_sandbox_allows_the_prefix_and_nothing_beside_it(
    tmp_path: Path, endpoint: str, bucket: str
) -> None:
    lake = s3_lake(tmp_path, endpoint, bucket)
    try:
        write(lake, READINGS)
        inside = next(key for key in keys(endpoint, bucket) if key.endswith(".parquet"))
        allowed = lake._connection.execute(
            f"SELECT count(*) FROM read_parquet('s3://{bucket}/{inside}')"
        ).fetchone()
        assert allowed is not None
        # Real Parquet files beside the prefix and at the bucket's root.
        local = tmp_path / "outside.parquet"
        plain = duckdb.connect()
        plain.execute(f"COPY (SELECT 42 AS secret_value) TO '{local}' (FORMAT parquet)")
        plain.close()
        for key in ["other/x.parquet", "x.parquet"]:
            put(endpoint, bucket, key, local.read_bytes())
        with pytest.raises(duckdb.PermissionException):
            lake._connection.execute(
                f"SELECT * FROM read_parquet('s3://{bucket}/other/x.parquet')"
            ).fetchall()
        with pytest.raises(duckdb.PermissionException):
            lake._connection.execute(
                f"SELECT * FROM read_parquet('s3://{bucket}/x.parquet')"
            ).fetchall()
        # A statement still cannot name a file at all.
        with pytest.raises(StoreError) as refused:
            write(lake, f"CREATE TABLE x AS SELECT * FROM read_parquet('s3://{bucket}/{inside}')")
        assert refused.value.code == "STORE_STATEMENT_INVALID"
    finally:
        lake.close()


@pytest.mark.xfail(
    strict=True,
    raises=pytest.fail.Exception,
    reason=(
        "DuckDB 2.0.0 checks an s3:// path against the sandbox as text, then resolves `..` "
        "before the request (measured in CI; moto itself does not resolve it). Known limitation, "
        "decision #140. Strict: this fails once DuckDB closes it, so the mark is removed then."
    ),
)
def test_a_dot_dot_path_cannot_leave_the_prefix(tmp_path: Path, endpoint: str, bucket: str) -> None:
    lake = s3_lake(tmp_path, endpoint, bucket)
    try:
        write(lake, READINGS)
        local = tmp_path / "outside.parquet"
        plain = duckdb.connect()
        plain.execute(f"COPY (SELECT 42 AS secret_value) TO '{local}' (FORMAT parquet)")
        plain.close()
        put(endpoint, bucket, "x.parquet", local.read_bytes())
        with pytest.raises(duckdb.Error):
            lake._connection.execute(
                f"SELECT * FROM read_parquet('s3://{bucket}/billing/../x.parquet')"
            ).fetchall()
    finally:
        lake.close()


def test_maintenance_on_object_storage_keeps_fresh_orphans(
    tmp_path: Path, endpoint: str, bucket: str
) -> None:
    lake = s3_lake(tmp_path, endpoint, bucket)
    orphan = "billing/main/readings/orphan.parquet"
    try:
        write(lake, READINGS)
        for _ in range(2):
            write(lake, READINGS.replace("CREATE TABLE readings AS", "INSERT INTO readings"))
        put(endpoint, bucket, orphan, b"not a lake file")
        now = dt.datetime.now(dt.UTC)
        # Within the grace period (the orphan is newer than the cutoff): kept.
        lake.maintain(now - dt.timedelta(days=1), now - dt.timedelta(hours=1))
        assert orphan in keys(endpoint, bucket)
        # Older than the cutoff: deleted, and only that.
        report = lake.maintain(now + dt.timedelta(minutes=1), now + dt.timedelta(hours=1))
        assert report.orphaned_files_deleted >= 1
        assert orphan not in keys(endpoint, bucket)
        assert read(lake, "SELECT count(*) AS n FROM readings") == [{"n": 6000}]
    finally:
        lake.close()


def test_plain_http_to_another_machine_is_refused(tmp_path: Path, bucket: str) -> None:
    location = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path=f"s3://{bucket}/billing/",
        storage=storage("storage.example.com", use_ssl=False),
    )
    with pytest.raises(StoreError) as refused:
        open_lake(location, LIMITS)
    assert refused.value.code == "STORE_TLS_REQUIRED"
    assert not (tmp_path / "lake").exists()  # refused before anything was opened


def test_unset_credentials_are_refused_before_opening(
    tmp_path: Path, endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    location = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path="s3://lake/billing/",
        storage=storage(endpoint),
    )
    with pytest.raises(StoreError) as refused:
        open_lake(location, LIMITS)
    assert refused.value.code == "STORE_SECRET_MISSING"
    assert refused.value.details == {"variable": KEY_ENV}


@pytest.mark.parametrize(
    ("data_path", "with_storage"),
    [
        ("s3://lake/billing/", False),
        ("/srv/lake/data/", True),
        ("gs://lake/billing/", True),
        ("s3://lake/billing/../", True),
        ("s3://lake/./billing/", True),
    ],
)
def test_storage_and_path_must_match(
    tmp_path: Path, endpoint: str, bucket: str, data_path: str, with_storage: bool
) -> None:
    location = SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(tmp_path / "catalog.sqlite"),
        data_path=data_path,
        storage=storage(endpoint) if with_storage else None,
    )
    with pytest.raises(StoreError) as refused:
        open_lake(location, LIMITS)
    assert refused.value.code == "STORE_LOCATION_INVALID"


def test_the_sandbox_lists_the_data_folder_or_prefix_exactly(tmp_path: Path) -> None:
    # #132's exact list, with #126's data locations: a local folder resolved, a prefix as written.
    local = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path=(tmp_path / "elsewhere").as_posix() + "/",
    )
    assert _sandbox_entries(local, "/spill") == (
        [
            "/spill",
            (tmp_path / "lake").resolve().as_posix(),
            (tmp_path / "elsewhere").resolve().as_posix(),
        ],
        [],
    )
    on_s3 = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path="s3://lake/billing/",
        storage=storage("127.0.0.1:9000"),
    )
    assert _sandbox_entries(on_s3, "/spill") == (
        ["/spill", (tmp_path / "lake").resolve().as_posix(), "s3://lake/billing/"],
        [],
    )


# --- statements never name an object (#136 review) -------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM 's3://lake/billing/../x.parquet'",
        "SELECT * FROM 's3://lake/billing/**/*.parquet'",
        "SELECT * FROM 'S3://lake/billing/x.parquet'",
        'SELECT * FROM "s3://lake/billing/x.parquet"',
        "CREATE TABLE leak AS SELECT * FROM 's3://lake/billing/../secret/*.csv'",
        "SELECT * FROM t JOIN 'gs://lake/x.csv' AS g USING (id)",
        "WITH w AS (SELECT * FROM 'az://c/p.parquet') SELECT * FROM w",
        "SUMMARIZE 'r2://lake/x.parquet'",
        "SELECT * FROM t WHERE id IN (SELECT id FROM 'abfss://c@a.dfs.core.windows.net/x')",
        "SELECT * FROM 'hf://datasets/x/y.parquet'",
        # Every quoting form (#153): dollar quotes with any tag, and E'' escapes.
        "SELECT * FROM (SELECT $$s3://lake/billing/../x.parquet$$ AS p)",
        "SELECT $q$gs://lake/x.csv$q$ AS p",
        "SELECT $ñ$az://c/p.parquet$ñ$ AS p",
        "SELECT E's3\\x3a//lake/x.parquet' AS p",
    ],
)
def test_a_statement_naming_an_object_is_refused(sql: str) -> None:
    # The first layer: the sandbox checks object paths as text only (#140), so a statement must
    # never name one. Checked without opening a lake.
    with pytest.raises(StoreError) as refused:
        _guarded(sql, frozenset())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'https://example.com/' || id AS link FROM t",
        "SELECT * FROM t WHERE note = 'see s3 docs'",
        "SELECT 'bucket: s3' AS label",
    ],
)
def test_ordinary_text_values_are_accepted(sql: str) -> None:
    _guarded(sql, frozenset())


@pytest.mark.parametrize(
    "target", ["billing/../x.parquet", "billing/**/*.parquet", "other/x.parquet"]
)
def test_a_statement_cannot_name_an_object(
    tmp_path: Path, endpoint: str, bucket: str, target: str
) -> None:
    # The same through a real lake on object storage, for reads and writes.
    lake = s3_lake(tmp_path, endpoint, bucket)
    try:
        write(lake, READINGS)
        named = f"SELECT * FROM 's3://{bucket}/{target}'"
        with pytest.raises(StoreError) as refused:
            read(lake, named)
        assert refused.value.code == "STORE_STATEMENT_INVALID"
        with pytest.raises(StoreError) as refused:
            write(lake, f"CREATE TABLE leak AS {named}")
        assert refused.value.code == "STORE_STATEMENT_INVALID"
    finally:
        lake.close()


@pytest.mark.parametrize("where", ["bucket root", "lake object", "elsewhere"])
def test_a_statement_cannot_name_a_web_address(
    tmp_path: Path, endpoint: str, bucket: str, where: str
) -> None:
    # Reviewer pass 4: httpfs opens http(s):// addresses too, including the storage server's own
    # address for an object. The statement check lets web addresses through as text, so the
    # lake's connection has the web file system turned off.
    lake = s3_lake(tmp_path, endpoint, bucket)
    try:
        write(lake, READINGS)
        root = tmp_path / "root.parquet"
        writer = duckdb.connect()
        writer.execute(f"COPY (SELECT 7 AS v) TO '{root}' (FORMAT parquet)")
        writer.close()
        put(endpoint, bucket, "x.parquet", root.read_bytes())
        lake_objects = [key for key in keys(endpoint, bucket) if key.endswith(".parquet")]
        lake_object = next(key for key in lake_objects if key.startswith("billing/"))
        address = {
            "bucket root": f"http://{endpoint}/{bucket}/x.parquet",
            "lake object": f"http://{endpoint}/{bucket}/{lake_object}",
            "elsewhere": "https://example.com/x.csv",
        }[where]
        before = lake.current_snapshot().id
        named = f"SELECT * FROM '{address}'"
        with pytest.raises(StoreError):
            read(lake, named)
        with pytest.raises(StoreError):
            write(lake, f"CREATE TABLE leak AS {named}")
        with pytest.raises(StoreError):
            write(lake, f"INSERT INTO readings SELECT * FROM '{address}'")
        assert lake.current_snapshot().id == before  # nothing was written
        assert read(lake, TOTAL) == EXPECTED
    finally:
        lake.close()


# --- encryption ------------------------------------------------------------------------------


def test_encrypted_files_cannot_be_read_without_the_catalog(
    tmp_path: Path, endpoint: str, bucket: str
) -> None:
    lake = s3_lake(tmp_path, endpoint, bucket, encrypted=True)
    try:
        write(lake, READINGS)
        assert read(lake, TOTAL) == EXPECTED  # read back exactly through the catalog
    finally:
        lake.close()
    files = [key for key in keys(endpoint, bucket) if key.endswith(".parquet")]
    assert files
    # A control object, not encrypted, through the same plain connection.
    control = tmp_path / "control.parquet"
    writer = duckdb.connect()
    writer.execute(f"COPY (SELECT 7 AS v) TO '{control}' (FORMAT parquet)")
    writer.close()
    put(endpoint, bucket, "control/x.parquet", control.read_bytes())
    plain = duckdb.connect()
    try:
        plain.execute("INSTALL httpfs")
        plain.execute("LOAD httpfs")
        plain.execute(
            f"CREATE SECRET (TYPE s3, KEY_ID '{KEY_ID}', SECRET '{SECRET}', REGION 'us-east-1', "
            f"ENDPOINT '{endpoint}', URL_STYLE 'path', USE_SSL false)"
        )
        # The connection reaches the bucket: a failure below is the encryption, not the setup.
        control_rows = plain.execute(
            f"SELECT * FROM read_parquet('s3://{bucket}/control/x.parquet')"
        ).fetchall()
        assert control_rows == [(7,)]
        for key in files:
            with pytest.raises(duckdb.Error):
                plain.execute(f"SELECT * FROM read_parquet('s3://{bucket}/{key}')").fetchall()
    finally:
        plain.close()
