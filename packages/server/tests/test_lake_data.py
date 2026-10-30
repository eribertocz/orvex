"""#126: the Builder chooses where the lake's data files live (`lake.data`), and whether they are
encrypted (`lake.encrypted`).

Validation tests run anywhere. The flows open real lakes (DuckLake, httpfs), so they run where
the extensions are available (CI); the S3 flow uses a local S3-compatible server (moto) on a
free port of 127.0.0.1 with disposable credentials.
"""

from __future__ import annotations

import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from moto.server import ThreadedMotoServer
from orvex.contracts.store.v1 import ReadRequest
from orvex.server import commands, lake_place
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.store.lake import Limits, open_lake

from tests.support import azurite

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
MODEL = "SELECT * FROM feed.data WHERE read_at > $since"
SIDECAR = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: merge, unique_key: [id], time_column: read_at, filter: manual}\n"
)
LIMITS = Limits("512MB", 1, 4, 16, 600_000)
KEY_ENV, SECRET_ENV = "LAKE_TEST_S3_KEY_ID", "LAKE_TEST_S3_SECRET"
SECRET = f"test-secret-{uuid.uuid4().hex}"
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def project(folder: Path, lake: str = "") -> Path:
    files = {
        "project.yaml": PROJECT + lake,
        "connections.yaml": CONNECTIONS,
        "models/s/m.sql": MODEL,
        "models/s/m.yaml": SIDECAR,
    }
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def bare(folder: Path, lake: str, connections: str | None = None) -> Path:
    """A project with no models: validating it opens no lake."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.yaml").write_text(PROJECT + lake, encoding="utf-8")
    if connections is not None:
        (folder / "connections.yaml").write_text(connections, encoding="utf-8")
    return folder


def feed(folder: Path, last: int) -> None:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute(
        f"COPY (SELECT i::BIGINT AS id, TIMESTAMPTZ '2026-10-01 00:00:00+00' + INTERVAL (i) HOUR "
        f"AS read_at FROM range(1, {last} + 1) AS r(i)) TO '{folder / 'data' / 'feed.parquet'}'"
    )
    connection.close()


def run(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str]:
    code = main(list(arguments))
    return code, capsys.readouterr().out


def home(folder: Path) -> Path:
    return folder / f".{SLUG}"


def problems(folder: Path) -> list[str]:
    return [str(problem) for problem in commands.check_project(folder).problems]


def count(folder: Path) -> int:
    place = commands.lake_location(load_project(folder), home(folder))
    lake = open_lake(place.location, LIMITS)
    try:
        request = ReadRequest(
            kind="read",
            sql="SELECT count(*) AS n FROM s.m",
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=16,
        )
        return int(lake.read(request).read_all().to_pylist()[0]["n"])
    finally:
        lake.close()


def full_flow(folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed(folder, 10)
    code, out = run(capsys, "run", str(folder))
    assert code == 0, out
    feed(folder, 15)
    code, out = run(capsys, "run", str(folder))
    assert code == 0, out
    assert count(folder) == 15
    code, out = run(capsys, "runs", str(folder))
    assert code == 0, out
    assert out.count("success") == 2
    code, out = run(capsys, "maintain", str(folder))
    assert code == 0, out
    assert "maintenance done" in out
    assert count(folder) == 15


# --- refused before anything is created ------------------------------------------------------


S3 = (
    "lake:\n  data:\n    path: s3://bi-lake/billing/\n    s3:\n      region: us-east-1\n"
    "      endpoint: '{endpoint}'\n      url_style: path\n      use_ssl: {use_ssl}\n"
    "      key_id: ${{env:" + KEY_ENV + "}}\n      secret: {secret}\n"
)


def s3_lake(
    endpoint: str, use_ssl: str = "false", secret: str = "${env:" + SECRET_ENV + "}"
) -> str:
    return S3.format(endpoint=endpoint, use_ssl=use_ssl, secret=secret)


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, "test-key-id")
    monkeypatch.setenv(SECRET_ENV, SECRET)


@pytest.mark.usefixtures("credentials")
def test_plain_http_to_a_remote_endpoint_is_refused(tmp_path: Path) -> None:
    found = problems(bare(tmp_path, s3_lake("storage.example.com")))
    assert len(found) == 1
    assert found[0].startswith("LAKE_TLS_REQUIRED project.yaml")
    assert SECRET not in found[0]
    assert problems(bare(tmp_path, s3_lake("storage.example.com", use_ssl="true"))) == []
    assert problems(bare(tmp_path, s3_lake("127.0.0.1:9000"))) == []
    assert problems(bare(tmp_path, s3_lake("[::1]:9000"))) == []


@pytest.mark.parametrize(
    "data",
    [
        "    path: gs://b/p/\n",
        "    path: https://h/p/\n",
        "    path: s3://b/p/\n",  # without its s3 settings
        "    path: data/elsewhere/\n    s3:\n      region: x\n      key_id: ${env:A}\n"
        "      secret: ${env:B}\n",
    ],
)
def test_unsupported_or_inconsistent_data_locations_are_refused(tmp_path: Path, data: str) -> None:
    found = problems(bare(tmp_path, f"lake:\n  data:\n{data}"))
    assert len(found) == 1
    assert found[0].startswith("LAKE_LOCATION_INVALID project.yaml"), found


def test_a_literal_secret_is_a_schema_error(tmp_path: Path) -> None:
    found = problems(bare(tmp_path, s3_lake("127.0.0.1:9000", secret="literal-secret")))
    assert found[0].startswith("PROJECT_SCHEMA_INVALID project.yaml")
    assert "literal-secret" not in found[0]


def test_an_unset_key_variable_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setenv(SECRET_ENV, SECRET)
    found = problems(bare(tmp_path, s3_lake("127.0.0.1:9000")))
    assert found[0].startswith("PROJECT_SECRET_MISSING project.yaml")
    assert KEY_ENV in found[0]


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("lake", "code"),
    [
        (s3_lake("storage.example.com"), "LAKE_TLS_REQUIRED"),
        ("lake:\n  data:\n    path: gs://b/p/\n", "LAKE_LOCATION_INVALID"),
        ("lake:\n  data:\n    path: https://h/p/\n", "LAKE_LOCATION_INVALID"),
        ("lake:\n  data:\n    path: s3://b/p/\n", "LAKE_LOCATION_INVALID"),
        (
            S3.replace("s3://bi-lake/billing/", "s3://bi-lake/billing/../").format(
                endpoint="127.0.0.1:9000", use_ssl="false", secret="${env:" + SECRET_ENV + "}"
            ),
            "LAKE_LOCATION_INVALID",
        ),
        (
            S3.replace("s3://bi-lake/billing/", "s3://bi-lake/./billing/").format(
                endpoint="127.0.0.1:9000", use_ssl="false", secret="${env:" + SECRET_ENV + "}"
            ),
            "LAKE_LOCATION_INVALID",
        ),
        (s3_lake("127.0.0.1:9000", secret="literal-secret"), "PROJECT_SCHEMA_INVALID"),
    ],
)
def test_a_refused_location_exits_1_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lake: str, code: str
) -> None:
    folder = project(tmp_path, lake)
    exit_code, out = run(capsys, "run", str(folder))
    assert exit_code == 1, out
    assert out.startswith(code), out
    assert SECRET not in out
    assert "literal-secret" not in out
    assert not home(folder).exists()


def test_an_unset_key_variable_exits_1_and_names_the_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setenv(SECRET_ENV, SECRET)
    folder = project(tmp_path, s3_lake("127.0.0.1:9000"))
    exit_code, out = run(capsys, "run", str(folder))
    assert exit_code == 1, out
    assert out.startswith("PROJECT_SECRET_MISSING"), out
    assert f"{KEY_ENV} is not set (lake.data.s3.key_id)" in out
    assert not home(folder).exists()


@pytest.mark.parametrize(
    ("data", "overlap"),
    [
        ("data", "contains the file of connection feed"),
        (".", "contains the project folder"),
        ("..", "contains the project folder"),
        (f".{SLUG}", "is inside"),
        (f".{SLUG}/lake/other", "is inside"),
    ],
)
def test_a_data_folder_must_hold_only_the_lakes_files(
    tmp_path: Path, data: str, overlap: str
) -> None:
    # DuckLake's orphan cleanup deletes every untracked .parquet file under the data folder,
    # so a folder shared with sources, the project or the lake's own files is refused.
    folder = bare(tmp_path / "proj", f"lake:\n  data:\n    path: {data}\n", CONNECTIONS)
    found = problems(folder)
    assert len(found) == 1, found
    assert found[0].startswith("LAKE_LOCATION_INVALID project.yaml"), found
    assert overlap in found[0]
    assert not home(folder).exists()


def test_a_data_folder_inside_a_wildcard_source_is_refused(tmp_path: Path) -> None:
    wildcard = (
        "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: readings/*.parquet\n"
    )
    folder = bare(tmp_path, "lake:\n  data:\n    path: readings/lake\n", wildcard)
    found = problems(folder)
    assert len(found) == 1, found
    assert "overlaps the files connection feed reads" in found[0]


def test_a_data_folder_of_its_own_is_accepted(tmp_path: Path) -> None:
    for data in ["lake-files", "data/lake", str(tmp_path / "elsewhere")]:
        folder = bare(tmp_path / "proj", f"lake:\n  data:\n    path: {data}\n", CONNECTIONS)
        assert problems(folder) == []


def test_changing_the_data_path_or_encryption_of_an_existing_lake_is_refused(
    tmp_path: Path,
) -> None:
    folder = bare(tmp_path / "proj", "")
    default = home(folder) / "lake" / "catalog.ducklake"
    default.parent.mkdir(parents=True)
    default.touch()  # an existing default lake
    for lake, changed in [
        (
            f"lake:\n  data:\n    path: {tmp_path / 'other'}\n",
            f"lake.data.path was the default folder, now {tmp_path.resolve() / 'other'}/",
        ),
        ("lake:\n  encrypted: true\n", "lake.encrypted was false, now true"),
    ]:
        bare(folder, lake)
        found = problems(folder)
        assert found[0].startswith("LAKE_LOCATION_CHANGED project.yaml"), found
        assert changed in found[0]  # the setting that changed, not lake.catalog
        assert "lake.catalog" not in found[0]
    bare(folder, f"lake:\n  data:\n    path: .{SLUG}/lake/data/\n")
    assert problems(folder) == []  # the default folder, named explicitly: the same lake


def test_a_changed_data_path_exits_1_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #126: "changing path after the lake exists" is among the refusals that create nothing.
    folder = bare(tmp_path / "proj", "")
    default = home(folder) / "lake" / "catalog.ducklake"
    default.parent.mkdir(parents=True)
    default.write_bytes(b"existing lake")
    other = tmp_path / "other"
    bare(folder, f"lake:\n  data:\n    path: {other}\n")
    code, out = run(capsys, "run", str(folder))
    assert code == 1, out
    assert out.startswith("LAKE_LOCATION_CHANGED"), out
    assert not other.exists()
    assert default.read_bytes() == b"existing lake"


# --- flows (CI) ------------------------------------------------------------------------------


def test_data_files_in_another_local_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "shared" / "billing"
    project(tmp_path, f"lake:\n  data:\n    path: {data}\n")
    full_flow(tmp_path, capsys)  # maintenance writes inlined rows out as files
    assert list(data.rglob("*.parquet"))
    assert not list((home(tmp_path) / "lake").rglob("*.parquet"))


@pytest.fixture
def endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"{host}:{port}"
    finally:
        server.stop()


def keys(endpoint: str, bucket: str) -> list[str]:
    with _HTTP.open(f"http://{endpoint}/{bucket}?list-type=2", timeout=30) as response:
        listed = ElementTree.fromstring(response.read())
    return [element.text or "" for element in listed.iter() if element.tag.endswith("}Key")]


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize("encrypted", [False, True])
def test_a_lake_on_object_storage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], endpoint: str, encrypted: bool
) -> None:
    request = urllib.request.Request(f"http://{endpoint}/bi-lake", method="PUT")
    _HTTP.open(request, timeout=30).close()
    lake = s3_lake(endpoint) + ("  encrypted: true\n" if encrypted else "")
    project(tmp_path, lake)
    full_flow(tmp_path, capsys)
    found = keys(endpoint, "bi-lake")
    assert found
    assert all(key.startswith("billing/") for key in found)  # the declared prefix only
    record = (home(tmp_path) / lake_place.RECORD_FILE).read_text(encoding="utf-8")
    assert SECRET not in record
    assert "test-key-id" not in record
    assert "s3://bi-lake/billing/" in record
    code, out = run(capsys, "runs", str(tmp_path))
    assert code == 0, out
    assert SECRET not in out
    chosen = commands.lake_location(load_project(tmp_path), home(tmp_path))
    # The setting reached the store (unset when false: the contract field is optional).
    assert bool(chosen.location.encrypted) is encrypted
    if encrypted:
        # Encryption is fixed when the lake is created: turning it off is refused.
        (tmp_path / "project.yaml").write_text(PROJECT + s3_lake(endpoint), encoding="utf-8")
        found = problems(tmp_path)
        assert found[0].startswith("LAKE_LOCATION_CHANGED project.yaml"), found
        assert "lake.encrypted was true, now false" in found[0]


# --- #127: Google Cloud Storage and Azure Blob Storage --------------------------------------------

AZURE_ENV = "LAKE_TEST_AZURE_CONNECTION_STRING"
ACCOUNT_KEY = f"azure-key-{uuid.uuid4().hex}"
GCS = (
    "lake:\n  data:\n    path: gs://bi-lake/billing/\n    gcs:\n"
    "      key_id: ${env:" + KEY_ENV + "}\n      secret: ${env:" + SECRET_ENV + "}\n"
)
AZURE = (
    "lake:\n  data:\n    path: az://bi-lake/billing/\n    azure:\n"
    "      connection_string: {connection}\n"
)


def azure_lake(connection: str = "${env:" + AZURE_ENV + "}") -> str:
    return AZURE.replace("{connection}", connection)


@pytest.mark.usefixtures("credentials")
def test_google_cloud_storage_is_reached_at_its_own_endpoint_over_https(tmp_path: Path) -> None:
    folder = bare(tmp_path, GCS)
    assert problems(folder) == []
    chosen = commands.lake_location(load_project(folder), home(folder))
    assert chosen.location.data_path == "gs://bi-lake/billing/"
    assert chosen.location.storage is not None
    storage = chosen.location.storage.root
    assert storage.model_dump(exclude_none=True) == {
        "type": "gcs",
        "endpoint": "storage.googleapis.com",
        "use_ssl": True,
        "key_id_env": KEY_ENV,
        "secret_env": SECRET_ENV,
    }


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    "data",
    [
        "    path: gcs://b/p/\n",
        "    path: azure://c/p/\n",
        "    path: abfss://c@a.dfs.core.windows.net/p/\n",
        "    path: r2://b/p/\n",
        "    path: gs://b/p/\n",  # without its gcs settings
        "    path: az://c/p/\n    gcs:\n      key_id: ${env:A}\n      secret: ${env:B}\n",
        "    path: gs://b/p/\n    azure:\n      connection_string: ${env:A}\n",
        "    path: gs:///p/\n    gcs:\n      key_id: ${env:A}\n      secret: ${env:B}\n",
        "    path: az://c/../p/\n    azure:\n      connection_string: ${env:A}\n",
        "    path: data/elsewhere/\n    azure:\n      connection_string: ${env:A}\n",
    ],
)
def test_unsupported_object_storage_locations_are_refused(tmp_path: Path, data: str) -> None:
    found = problems(bare(tmp_path, f"lake:\n  data:\n{data}"))
    assert len(found) == 1
    assert found[0].startswith("LAKE_LOCATION_INVALID project.yaml"), found


def test_a_literal_connection_string_is_a_schema_error(tmp_path: Path) -> None:
    literal = f"DefaultEndpointsProtocol=https;AccountName=a;AccountKey={ACCOUNT_KEY}"
    found = problems(bare(tmp_path, azure_lake(f"'{literal}'")))
    assert found[0].startswith("PROJECT_SCHEMA_INVALID project.yaml")
    assert ACCOUNT_KEY not in found[0]


def test_an_unset_connection_string_variable_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AZURE_ENV, raising=False)
    found = problems(bare(tmp_path, azure_lake()))
    assert found[0].startswith("PROJECT_SECRET_MISSING project.yaml")
    assert AZURE_ENV in found[0]


@pytest.mark.parametrize(
    ("connection", "refused"),
    [
        (f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY}", True),
        (f"AccountName=a;AccountKey={ACCOUNT_KEY};BlobEndpoint=http://blob.example.com/a", True),
        (f"DefaultEndpointsProtocol=https;AccountName=a;AccountKey={ACCOUNT_KEY}", False),
        (
            f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY};"
            "BlobEndpoint=http://127.0.0.1:10000/a",
            False,
        ),
    ],
)
def test_azure_over_plain_http_to_another_machine_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection: str, refused: bool
) -> None:
    monkeypatch.setenv(AZURE_ENV, connection)
    found = problems(bare(tmp_path, azure_lake()))
    if not refused:
        assert found == []
        return
    assert len(found) == 1
    assert found[0].startswith("LAKE_TLS_REQUIRED project.yaml"), found
    assert ACCOUNT_KEY not in found[0]
    assert "http" in found[0]


@pytest.mark.usefixtures("credentials")
def test_moving_an_existing_lake_to_another_provider_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = bare(tmp_path / "proj", s3_lake("127.0.0.1:9000"))
    lake_place.record(home(folder), commands.lake_location(load_project(folder), home(folder)))
    bare(folder, GCS)
    found = problems(folder)
    assert found[0].startswith("LAKE_LOCATION_CHANGED project.yaml"), found
    assert "s3://bi-lake/billing/" in found[0]
    assert "gs://bi-lake/billing/" in found[0]
    monkeypatch.setenv(AZURE_ENV, "AccountName=a;AccountKey=k")
    bare(folder, azure_lake())
    assert problems(folder)[0].startswith("LAKE_LOCATION_CHANGED project.yaml")


def test_a_lake_in_azure_blob_storage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = azurite.connection_string()
    monkeypatch.setenv(AZURE_ENV, connection)
    container = f"lake{uuid.uuid4().hex[:12]}"
    azurite.create_container(connection, container)
    project(tmp_path, azure_lake().replace("az://bi-lake/", f"az://{container}/"))
    full_flow(tmp_path, capsys)
    found = azurite.blobs(connection, container)
    assert found
    assert all(name.startswith("billing/") for name in found)  # the declared prefix only
    account_key = connection.split("AccountKey=", 1)[1].split(";", 1)[0]
    record = (home(tmp_path) / lake_place.RECORD_FILE).read_text(encoding="utf-8")
    assert account_key not in record
    assert f"az://{container}/billing/" in record
    code, out = run(capsys, "runs", str(tmp_path))
    assert code == 0, out
    assert account_key not in out


@pytest.mark.parametrize(
    "connection",
    [
        f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY};"
        "blobendpoint=http://127.0.0.1:10000/a",
        f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY};"
        "BlobEndpoint =http://127.0.0.1:10000/a",
        f"DefaultEndpointsProtocol=http;AccountName=a;AccountKey={ACCOUNT_KEY};"
        "defaultendpointsprotocol=https",
        f"BlobEndpoint=http://evil.example.com/a;AccountName=a;AccountKey={ACCOUNT_KEY};"
        "blobendpoint=http://localhost/a",
        f"DefaultEndpointsProtocol=https;AccountName=a;AccountKey={ACCOUNT_KEY};"
        "DefaultEndpointsProtocol=http",
    ],
)
def test_an_azure_connection_string_is_read_as_the_sdk_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection: str
) -> None:
    # #171 Reviewer pass 1: keys by case, untrimmed, written once.
    monkeypatch.setenv(AZURE_ENV, connection)
    found = problems(bare(tmp_path, azure_lake()))
    assert len(found) == 1
    assert found[0].startswith("LAKE_TLS_REQUIRED project.yaml"), found
    assert ACCOUNT_KEY not in found[0]


@pytest.mark.usefixtures("credentials")
def test_the_gcs_record_holds_no_credential(tmp_path: Path) -> None:
    folder = bare(tmp_path, GCS)
    lake_place.record(home(folder), commands.lake_location(load_project(folder), home(folder)))
    text = (home(folder) / lake_place.RECORD_FILE).read_text(encoding="utf-8")
    assert "gs://bi-lake/billing/" in text
    assert SECRET not in text
    assert "test-key-id" not in text
