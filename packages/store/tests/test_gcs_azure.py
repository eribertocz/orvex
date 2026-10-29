"""#127: the lake's data files in Google Cloud Storage (`gs://`) and Azure Blob Storage
(`az://`), with the guarantees of S3 (#126).

Google Cloud Storage is reached through DuckDB's `gcs` secret, which speaks Cloud Storage's
S3-compatible XML API: these tests point it at a local S3-compatible server (moto), which
exercises DuckDB's `gs://` handling end to end. Azure runs against Azurite, Azure Storage's
emulator, started by CI. The refusal tests open nothing and run anywhere.
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
    AzureStorage,
    GcsStorage,
    LocalLakeLocation,
    ObjectStorage,
    ReadRequest,
    S3Storage,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

from tests.support import azurite

LIMITS = Limits(
    memory_limit="512MB", threads=1, max_concurrent=4, queue_size=16, default_timeout_ms=600_000
)
KEY_ENV, SECRET_ENV = "LAKE_TEST_GCS_KEY_ID", "LAKE_TEST_GCS_SECRET"
AZURE_ENV = "LAKE_TEST_AZURE_CONNECTION_STRING"
KEY_ID, SECRET = "test-hmac-key", f"test-hmac-secret-{uuid.uuid4().hex}"
READINGS = "CREATE TABLE readings AS SELECT range AS id, range * 1.5 AS kwh FROM range(2000)"
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def write(lake: Lake, sql: str) -> None:
    with lake.transaction() as transaction:
        transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))


def read(lake: Lake, sql: str) -> list[dict[str, object]]:
    request = ReadRequest(
        kind="read", sql=sql, parameters=[], snapshot=lake.current_snapshot().id, batch_size=64
    )
    return list(lake.read(request).read_all().to_pylist())


def location(
    tmp_path: Path,
    data_path: str,
    storage: S3Storage | GcsStorage | AzureStorage,
    encrypted: bool | None = None,
) -> LocalLakeLocation:
    return LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        data_path=data_path,
        storage=ObjectStorage(storage),
        encrypted=encrypted,
    )


def code_of(action: object) -> StoreError:
    assert callable(action)
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value


# --- refused before anything is opened --------------------------------------------------------


def gcs(endpoint: str = "storage.googleapis.com", use_ssl: bool = True) -> GcsStorage:
    return GcsStorage(
        type="gcs", endpoint=endpoint, use_ssl=use_ssl, key_id_env=KEY_ENV, secret_env=SECRET_ENV
    )


def azure() -> AzureStorage:
    return AzureStorage(type="azure", connection_string_env=AZURE_ENV)


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, KEY_ID)
    monkeypatch.setenv(SECRET_ENV, SECRET)


ACCOUNT_KEY = f"key-{uuid.uuid4().hex}"


@pytest.mark.parametrize(
    "connection",
    [
        f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY}",
        f"AccountName=a;AccountKey={ACCOUNT_KEY};BlobEndpoint=http://blob.example.com/a",
        f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY};"
        "BlobEndpoint=http://10.0.0.5:10000/a",
    ],
)
def test_azure_over_plain_http_to_another_machine_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection: str
) -> None:
    monkeypatch.setenv(AZURE_ENV, connection)
    error = code_of(lambda: open_lake(location(tmp_path, "az://c/p/", azure()), LIMITS))
    assert error.code == "STORE_TLS_REQUIRED"
    assert ACCOUNT_KEY not in str(error.message) + str(error.details)
    assert not (tmp_path / "lake").exists()


@pytest.mark.parametrize(
    ("connection", "code"),
    [
        (f"AccountName=a;AccountKey={ACCOUNT_KEY}", None),  # https by default
        (f"DefaultEndpointsProtocol=https;AccountName=a;AccountKey={ACCOUNT_KEY}", None),
        (
            f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY};"
            "BlobEndpoint=http://127.0.0.1:10000/a",
            None,
        ),
        (f"AccountKey={ACCOUNT_KEY};BlobEndpoint=https://blob.example.com/a", "missing-account"),
        (f"not a connection string {ACCOUNT_KEY}", "not-pairs"),
    ],
)
def test_azure_connection_strings_are_checked_without_being_shown(
    connection: str, code: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orvex.store.lake import _storage_credentials

    monkeypatch.setenv(AZURE_ENV, connection)
    if code is None:
        found = _storage_credentials(ObjectStorage(azure()), "az://c/p/")
        assert found is not None
        assert found.key_id == connection
        return
    error = code_of(lambda: _storage_credentials(ObjectStorage(azure()), "az://c/p/"))
    assert error.code == "STORE_STATEMENT_INVALID"
    assert ACCOUNT_KEY not in str(error.message) + str(error.details)


@pytest.mark.usefixtures("credentials")
def test_gcs_over_plain_http_to_another_machine_is_refused(tmp_path: Path) -> None:
    error = code_of(lambda: open_lake(location(tmp_path, "gs://b/p/", gcs(use_ssl=False)), LIMITS))
    assert error.code == "STORE_TLS_REQUIRED"
    assert not (tmp_path / "lake").exists()


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("data_path", "storage"),
    [
        ("gs://b/p/", "s3"),  # a scheme with another kind of storage
        ("az://c/p/", "gcs"),
        ("s3://b/p/", "azure"),
        ("gcs://b/p/", "gcs"),  # not the accepted spelling
        ("azure://c/p/", "azure"),
        ("abfss://c@a.dfs.core.windows.net/p/", "azure"),
        ("gs://b/p/../q/", "gcs"),
        ("az://c/./p/", "azure"),
    ],
)
def test_inconsistent_object_storage_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_path: str, storage: str
) -> None:
    monkeypatch.setenv(AZURE_ENV, "AccountName=a;AccountKey=k")
    settings: S3Storage | GcsStorage | AzureStorage
    if storage == "s3":
        settings = S3Storage(
            type="s3", region="us-east-1", use_ssl=True, key_id_env=KEY_ENV, secret_env=SECRET_ENV
        )
    elif storage == "gcs":
        settings = gcs()
    else:
        settings = azure()
    error = code_of(lambda: open_lake(location(tmp_path, data_path, settings), LIMITS))
    assert error.code == "STORE_LOCATION_INVALID", error.message


def test_an_unset_connection_string_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AZURE_ENV, raising=False)
    error = code_of(lambda: open_lake(location(tmp_path, "az://c/p/", azure()), LIMITS))
    assert error.code == "STORE_SECRET_MISSING"
    assert error.details == {"variable": AZURE_ENV}


# --- Google Cloud Storage, through a local S3-compatible server ---------------------------------


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"{host}:{port}"
    finally:
        server.stop()


def bucket_keys(endpoint: str, bucket: str) -> list[str]:
    with _HTTP.open(f"http://{endpoint}/{bucket}?list-type=2", timeout=30) as response:
        listed = ElementTree.fromstring(response.read())
    return [element.text or "" for element in listed.iter() if element.tag.endswith("}Key")]


def put(endpoint: str, bucket: str, key: str, body: bytes) -> None:
    request = urllib.request.Request(
        f"http://{endpoint}/{bucket}/{key}",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
    )
    _HTTP.open(request, timeout=30).close()


@pytest.fixture
def bucket(endpoint: str, credentials: None) -> str:
    name = f"lake-{uuid.uuid4().hex[:12]}"
    _HTTP.open(urllib.request.Request(f"http://{endpoint}/{name}", method="PUT"), timeout=30)
    return name


def parquet(tmp_path: Path, value: int) -> bytes:
    path = tmp_path / f"outside-{value}.parquet"
    plain = duckdb.connect()
    plain.execute(f"COPY (SELECT {value} AS secret_value) TO '{path}' (FORMAT parquet)")
    plain.close()
    return path.read_bytes()


def flow(lake_at: LocalLakeLocation) -> None:
    """Write, read back exactly, reopen, maintain: the #126 flow on another provider."""
    lake = open_lake(lake_at, LIMITS)
    try:
        write(lake, READINGS)
        assert read(lake, "SELECT count(*) AS n, sum(kwh) AS total FROM readings") == [
            {"n": 2000, "total": pytest.approx(2998500.0)}
        ]
    finally:
        lake.close()
    lake = open_lake(lake_at, LIMITS)
    try:
        assert read(lake, "SELECT count(*) AS n FROM readings") == [{"n": 2000}]
        write(lake, READINGS.replace("CREATE TABLE readings AS", "INSERT INTO readings"))
        assert read(lake, "SELECT count(*) AS n FROM readings") == [{"n": 4000}]
    finally:
        lake.close()


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_lake_in_google_cloud_storage(
    tmp_path: Path, endpoint: str, bucket: str, encrypted: bool
) -> None:
    lake_at = location(
        tmp_path, f"gs://{bucket}/billing/", gcs(endpoint, use_ssl=False), encrypted=encrypted
    )
    flow(lake_at)
    found = bucket_keys(endpoint, bucket)
    assert found
    assert all(key.startswith("billing/") for key in found)  # the declared prefix only
    put(endpoint, bucket, "beside.parquet", parquet(tmp_path, 42))
    lake = open_lake(lake_at, LIMITS)
    try:
        # The sandbox refuses an object beside the prefix (the statement check is bypassed).
        with pytest.raises(duckdb.PermissionException):
            lake._connection.execute(f"SELECT * FROM 'gs://{bucket}/beside.parquet'").fetchall()
        secrets = lake._connection.execute("SELECT * FROM duckdb_secrets()").fetchall()
        assert SECRET not in str(secrets)
        assert lake._connection.execute(
            "SELECT current_setting('allow_persistent_secrets')"
        ).fetchall() == [(False,)]
        # Maintenance keeps a fresh orphan and deletes an old one, under the prefix only.
        orphan = "billing/main/readings/orphan.parquet"
        put(endpoint, bucket, orphan, b"not a lake file")
        now = dt.datetime.now(dt.UTC)
        lake.maintain(now - dt.timedelta(days=1), now - dt.timedelta(hours=1))
        assert orphan in bucket_keys(endpoint, bucket)
        lake.maintain(now + dt.timedelta(minutes=1), now + dt.timedelta(hours=1))
        assert orphan not in bucket_keys(endpoint, bucket)
        assert "beside.parquet" in bucket_keys(endpoint, bucket)
        assert read(lake, "SELECT count(*) AS n FROM readings") == [{"n": 4000}]
    finally:
        lake.close()
    if encrypted:
        # Plain DuckDB, with the same credentials, cannot read a data file.
        data = next(key for key in bucket_keys(endpoint, bucket) if "ducklake-" in key)
        plain = duckdb.connect()
        plain.execute("INSTALL httpfs")
        plain.execute("LOAD httpfs")
        plain.execute(
            f"CREATE SECRET (TYPE gcs, KEY_ID '{KEY_ID}', SECRET '{SECRET}', "
            f"ENDPOINT '{endpoint}', USE_SSL false, URL_STYLE 'path')"
        )
        with pytest.raises(duckdb.Error):
            plain.execute(f"SELECT * FROM 'gs://{bucket}/{data}'").fetchall()
        plain.close()


# --- Azure Blob Storage, against Azurite ---------------------------------------------------------


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> str:
    connection = azurite.connection_string()
    monkeypatch.setenv(AZURE_ENV, connection)
    name = f"lake{uuid.uuid4().hex[:12]}"
    azurite.create_container(connection, name)
    return name


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_lake_in_azure_blob_storage(tmp_path: Path, container: str, encrypted: bool) -> None:
    connection = azurite.connection_string()
    lake_at = location(tmp_path, f"az://{container}/billing/", azure(), encrypted=encrypted)
    flow(lake_at)
    found = azurite.blobs(connection, container)
    assert found
    assert all(name.startswith("billing/") for name in found)  # the declared prefix only
    azurite.put_blob(connection, container, "beside.parquet", parquet(tmp_path, 42))
    account_key = connection.split("AccountKey=", 1)[1].split(";", 1)[0]
    lake = open_lake(lake_at, LIMITS)
    try:
        with pytest.raises(duckdb.PermissionException):
            lake._connection.execute(f"SELECT * FROM 'az://{container}/beside.parquet'").fetchall()
        secrets = str(lake._connection.execute("SELECT * FROM duckdb_secrets()").fetchall())
        assert account_key not in secrets
        assert connection not in secrets
        orphan = "billing/main/readings/orphan.parquet"
        azurite.put_blob(connection, container, orphan, b"not a lake file")
        now = dt.datetime.now(dt.UTC)
        lake.maintain(now - dt.timedelta(days=1), now - dt.timedelta(hours=1))
        assert orphan in azurite.blobs(connection, container)
        lake.maintain(now + dt.timedelta(minutes=1), now + dt.timedelta(hours=1))
        assert orphan not in azurite.blobs(connection, container)
        assert "beside.parquet" in azurite.blobs(connection, container)
        assert read(lake, "SELECT count(*) AS n FROM readings") == [{"n": 4000}]
    finally:
        lake.close()
    if encrypted:
        data = next(name for name in azurite.blobs(connection, container) if "ducklake-" in name)
        plain = duckdb.connect()
        plain.execute("INSTALL azure")
        plain.execute("LOAD azure")
        plain.execute(f"CREATE SECRET (TYPE azure, CONNECTION_STRING '{connection}')")
        with pytest.raises(duckdb.Error):
            plain.execute(f"SELECT * FROM 'az://{container}/{data}'").fetchall()
        plain.close()


def test_an_azure_error_never_shows_the_connection_string(
    tmp_path: Path, container: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = azurite.connection_string()
    account_key = connection.split("AccountKey=", 1)[1].split(";", 1)[0]
    # A wrong key: the emulator refuses the first write; the error shows neither key.
    wrong = connection.replace(account_key, "d3Jvbmcta2V5LWZvci10aGUtdGVzdA==")
    monkeypatch.setenv(AZURE_ENV, wrong)
    lake = open_lake(location(tmp_path, f"az://{container}/billing/", azure()), LIMITS)
    try:
        error = code_of(lambda: write(lake, READINGS))
    finally:
        lake.close()
    text = str(error.message) + str(error.details)
    assert "d3Jvbmcta2V5LWZvci10aGUtdGVzdA==" not in text
    assert account_key not in text
    assert wrong not in text


def test_an_encrypted_lake_on_a_local_disk_is_written_and_unreadable_alone(tmp_path: Path) -> None:
    # Found with #127: DuckDB writes encrypted files only with httpfs's crypto module loaded;
    # a local encrypted lake (#126) failed its first write.
    lake_at = LocalLakeLocation(
        kind="lake_location", catalog="local", path=str(tmp_path / "lake"), encrypted=True
    )
    flow(lake_at)
    lake = open_lake(lake_at, LIMITS)
    try:
        # httpfs's web file systems stay off.
        with pytest.raises(duckdb.Error):
            lake._connection.execute("SELECT * FROM 'https://example.com/x.parquet'").fetchall()
    finally:
        lake.close()
    data = next((tmp_path / "lake" / "data").rglob("ducklake-*.parquet"))
    plain = duckdb.connect()
    with pytest.raises(duckdb.Error):
        plain.execute(f"SELECT * FROM '{data}'").fetchall()
    plain.close()
