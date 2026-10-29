"""TEMPORARY probe for #127 (removed before the pull request is ready): measures what DuckDB's
gcs secret and azure extension do against the local test servers, and reports it as a failure
message so CI annotations carry it."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import duckdb
import pytest
from moto.server import ThreadedMotoServer

_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"{host}:{port}"
    finally:
        server.stop()


def attempt(results: dict[str, str], name: str, action: Callable[[], object]) -> object:
    try:
        value = action()
    except Exception as error:  # noqa: BLE001 - a probe records every outcome
        results[name] = f"ERR {type(error).__name__}: {str(error)[:160]}"
        return None
    results[name] = f"OK {str(value)[:120]}"
    return value


def test_probe_gcs(endpoint: str, tmp_path: Path) -> None:
    results: dict[str, str] = {}
    bucket = f"g-{uuid.uuid4().hex[:10]}"
    _HTTP.open(urllib.request.Request(f"http://{endpoint}/{bucket}", method="PUT"), timeout=30)
    c = duckdb.connect()
    attempt(results, "load", lambda: c.execute("INSTALL httpfs; LOAD httpfs; INSTALL ducklake; LOAD ducklake"))
    attempt(
        results,
        "secret",
        lambda: c.execute(
            f"CREATE TEMPORARY SECRET g (TYPE gcs, KEY_ID 'k', SECRET 's', ENDPOINT '{endpoint}', "
            f"USE_SSL false, URL_STYLE 'path', SCOPE 'gs://{bucket}/billing/')"
        ),
    )
    attempt(results, "secret_row", lambda: c.execute("SELECT type, provider, scope FROM duckdb_secrets()").fetchall())
    attempt(results, "copy", lambda: c.execute(f"COPY (SELECT 42 AS a) TO 'gs://{bucket}/billing/x.parquet'"))
    attempt(results, "read", lambda: c.execute(f"SELECT * FROM 'gs://{bucket}/billing/x.parquet'").fetchall())
    plain = duckdb.connect()
    plain.execute(f"COPY (SELECT 7 AS a) TO '{tmp_path}/root.parquet'")
    plain.close()
    body = (tmp_path / "root.parquet").read_bytes()
    attempt(results, "copy_root", lambda: _HTTP.open(urllib.request.Request(f"http://{endpoint}/{bucket}/root.parquet", data=body, method="PUT", headers={"Content-Type": "application/octet-stream"}), timeout=30).status)
    attempt(
        results,
        "listing",
        lambda: _HTTP.open(f"http://{endpoint}/{bucket}?list-type=2", timeout=30).read()[-300:],
    )
    attempt(
        results,
        "ducklake",
        lambda: c.execute(
            f"ATTACH 'ducklake:{tmp_path}/c.ducklake' AS lake (DATA_PATH 'gs://{bucket}/billing/');"
            "CREATE TABLE lake.t AS SELECT range AS a FROM range(1000);"
            "SELECT count(*) FROM lake.t"
        ).fetchall(),
    )
    attempt(results, "ducklake_files", lambda: c.execute("SELECT data_file FROM ducklake_list_files('lake', 't')").fetchall())
    attempt(results, "sandbox_set", lambda: c.execute(f"SET allowed_directories = ['gs://{bucket}/billing/']; SET enable_external_access = false"))
    attempt(results, "sandbox_inside", lambda: c.execute(f"SELECT * FROM 'gs://{bucket}/billing/x.parquet'").fetchall())
    attempt(results, "sandbox_outside", lambda: c.execute(f"SELECT * FROM 'gs://{bucket}/root.parquet'").fetchall())
    attempt(results, "sandbox_dotdot", lambda: c.execute(f"SELECT * FROM 'gs://{bucket}/billing/../root.parquet'").fetchall())
    attempt(results, "sandbox_s3_alias", lambda: c.execute(f"SELECT * FROM 's3://{bucket}/root.parquet'").fetchall())
    attempt(results, "sandbox_gcs_alias", lambda: c.execute(f"SELECT * FROM 'gcs://{bucket}/root.parquet'").fetchall())
    attempt(results, "ducklake_after", lambda: c.execute("INSERT INTO lake.t SELECT 1; SELECT count(*) FROM lake.t").fetchall())
    c.close()
    pytest.fail("PROBE-GCS " + json.dumps(results))


ACCOUNT = "devstoreaccount1"


def azure_put_container(connection_string: str, container: str) -> str:
    parts = dict(item.split("=", 1) for item in connection_string.strip(";").split(";"))
    key = base64.b64decode(parts["AccountKey"])
    endpoint = parts["BlobEndpoint"].rstrip("/")
    date = dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    version = "2021-08-06"
    canonical_headers = f"x-ms-date:{date}\nx-ms-version:{version}\n"
    resource = f"/{ACCOUNT}/{ACCOUNT}/{container}\nrestype:container"
    # VERB, then Content-Encoding, -Language, -Length (empty for 0), -MD5, -Type, Date,
    # If-Modified-Since, If-Match, If-None-Match, If-Unmodified-Since, Range.
    to_sign = "PUT\n\n\n\n\napplication/octet-stream\n\n\n\n\n\n\n" + canonical_headers + resource
    signature = base64.b64encode(hmac.new(key, to_sign.encode(), hashlib.sha256).digest()).decode()
    request = urllib.request.Request(
        f"{endpoint}/{container}?restype=container",
        method="PUT",
        headers={
            "x-ms-date": date,
            "x-ms-version": version,
            "Authorization": f"SharedKey {ACCOUNT}:{signature}",
            "Content-Length": "0",
            "Content-Type": "application/octet-stream",
        },
        data=b"",
    )
    with _HTTP.open(request, timeout=30) as response:
        return str(response.status)


def test_probe_azure(tmp_path: Path) -> None:
    results: dict[str, str] = {}
    connection_string = os.environ.get("STORE_TEST_AZURE_CONNECTION_STRING", "")
    if not connection_string:
        pytest.skip("no Azurite")
    container = f"a{uuid.uuid4().hex[:10]}"
    attempt(results, "container", lambda: azure_put_container(connection_string, container))
    c = duckdb.connect()
    attempt(results, "load", lambda: c.execute("INSTALL azure; LOAD azure; INSTALL ducklake; LOAD ducklake"))
    attempt(
        results,
        "secret",
        lambda: c.execute(
            f"CREATE TEMPORARY SECRET a (TYPE azure, CONNECTION_STRING '{connection_string}', "
            f"SCOPE 'az://{container}/billing/')"
        ),
    )
    attempt(results, "secret_row", lambda: c.execute("SELECT type, provider, scope FROM duckdb_secrets()").fetchall())
    attempt(results, "copy", lambda: c.execute(f"COPY (SELECT 42 AS a) TO 'az://{container}/billing/x.parquet'"))
    attempt(results, "read", lambda: c.execute(f"SELECT * FROM 'az://{container}/billing/x.parquet'").fetchall())
    attempt(results, "copy_root", lambda: c.execute(f"COPY (SELECT 7 AS a) TO 'az://{container}/root.parquet'"))
    attempt(
        results,
        "ducklake",
        lambda: c.execute(
            f"ATTACH 'ducklake:{tmp_path}/c.ducklake' AS lake (DATA_PATH 'az://{container}/billing/');"
            "CREATE TABLE lake.t AS SELECT range AS a FROM range(1000);"
            "SELECT count(*) FROM lake.t"
        ).fetchall(),
    )
    attempt(results, "ducklake_files", lambda: c.execute("SELECT data_file FROM ducklake_list_files('lake', 't')").fetchall())
    attempt(results, "glob", lambda: c.execute(f"SELECT file FROM glob('az://{container}/billing/**')").fetchall())
    attempt(results, "sandbox_set", lambda: c.execute(f"SET allowed_directories = ['az://{container}/billing/']; SET enable_external_access = false"))
    attempt(results, "sandbox_inside", lambda: c.execute(f"SELECT * FROM 'az://{container}/billing/x.parquet'").fetchall())
    attempt(results, "sandbox_outside", lambda: c.execute(f"SELECT * FROM 'az://{container}/root.parquet'").fetchall())
    attempt(results, "sandbox_dotdot", lambda: c.execute(f"SELECT * FROM 'az://{container}/billing/../root.parquet'").fetchall())
    attempt(results, "sandbox_azure_alias", lambda: c.execute(f"SELECT * FROM 'azure://{container}/root.parquet'").fetchall())
    attempt(results, "ducklake_after", lambda: c.execute("INSERT INTO lake.t SELECT 1; SELECT count(*) FROM lake.t").fetchall())
    attempt(results, "delete_orphans", lambda: c.execute("CALL ducklake_delete_orphaned_files('lake', cleanup_all => true)").fetchall())
    c.close()
    pytest.fail("PROBE-AZURE " + json.dumps(results))
