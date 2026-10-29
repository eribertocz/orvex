"""#120: the Builder chooses where the lake's catalog lives (`lake.catalog` in `project.yaml`).

Validation tests run anywhere. The flows open real lakes (DuckLake), so they run where the
extensions are available (CI). The PostgreSQL flows need `STORE_TEST_POSTGRES` (+ password)
and, for TLS, `STORE_TEST_POSTGRES_TLS` and `STORE_TEST_POSTGRES_TLS_CA`; CI provides them.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import ReadRequest
from orvex.server import commands, lake_place
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.store.lake import Limits, open_lake

SLUG = load_identity().slug
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
MODEL = "SELECT * FROM feed.data WHERE read_at > $since"
SIDECAR = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: merge, unique_key: [id], time_column: read_at, filter: manual}\n"
)
LIMITS = Limits("512MB", 1, 4, 16, 600_000)


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


def feed(folder: Path, last: int) -> None:
    """Readings 1 to `last`, one per hour from 2026-10-01 00:00 UTC."""
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


def count(folder: Path) -> int:
    """The model's rows, read through the project's own lake location."""
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
    """Two incremental runs, the run history, maintenance and a read-back."""
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
    assert "maintenance done" in out  # it ran on this catalog, not "nothing to maintain"
    assert count(folder) == 15
    data = home(folder) / "lake" / "data"
    assert list(data.rglob("*.parquet"))


def problem(capsys: pytest.CaptureFixture[str], folder: Path) -> str:
    code, out = run(capsys, "validate", str(folder))
    assert code == 1, out
    return out


# --- mountinfo -------------------------------------------------------------------------------

MOUNTINFO = """\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
30 22 259:3 / /data rw,relatime shared:2 - xfs /dev/nvme0n1p3 rw
31 22 0:40 / /mnt/nfs rw,relatime shared:3 - nfs4 server:/export rw,vers=4.2
32 22 0:41 / /mnt/smb rw,relatime shared:4 - cifs //server/share rw
33 22 0:42 / /mnt/ssh rw,nosuid shared:5 - fuse.sshfs user@host:/srv rw
34 31 259:4 / /mnt/nfs/local rw,relatime shared:6 - ext4 /dev/nvme0n1p4 rw
35 22 0:43 / /mnt/with\\040space rw - cifs //s/x rw
"""


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/home/u/project/catalog.ducklake", "ext4"),
        ("/data/lake/catalog.sqlite", "xfs"),
        ("/mnt/nfs/team/catalog.ducklake", "nfs4"),
        ("/mnt/smb/catalog.ducklake", "cifs"),
        ("/mnt/ssh/x/catalog.ducklake", "fuse.sshfs"),
        ("/mnt/nfs/local/catalog.ducklake", "ext4"),  # a local disk mounted inside an NFS mount
        ("/mnt/with space/catalog.ducklake", "cifs"),
    ],
)
def test_the_file_system_of_a_path(path: str, kind: str) -> None:
    assert lake_place.mount_type(Path(path), MOUNTINFO) == kind


# --- refused before anything is created ------------------------------------------------------


@pytest.mark.parametrize("kind", ["duckdb", "sqlite"])
@pytest.mark.parametrize("url", ["s3://b/c.ducklake", "gs://b/c", "https://h/c.ducklake"])
def test_a_catalog_file_in_object_storage_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, url: str
) -> None:
    project(tmp_path, f"lake:\n  catalog:\n    type: {kind}\n    path: {url}\n")
    out = problem(capsys, tmp_path)
    assert out.startswith("LAKE_LOCATION_INVALID project.yaml")
    assert not home(tmp_path).exists()


@pytest.mark.parametrize("kind", ["nfs4", "cifs", "fuse.sshfs"])
def test_a_catalog_on_a_network_file_system_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(lake_place, "file_system", lambda path: kind)
    project(tmp_path, f"lake:\n  catalog:\n    type: sqlite\n    path: {tmp_path / 'c.sqlite'}\n")
    out = problem(capsys, tmp_path)
    assert out.startswith("LAKE_LOCATION_INVALID")
    assert kind in out
    assert not (tmp_path / "c.sqlite").exists()


def test_a_platform_without_detection_keeps_the_default_path_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lake_place, "file_system", lambda path: None)
    project(tmp_path, f"lake:\n  catalog:\n    type: duckdb\n    path: {tmp_path / 'c.ducklake'}\n")
    assert problem(capsys, tmp_path).startswith("LAKE_LOCATION_INVALID")
    feed(tmp_path, 3)
    project(tmp_path, "lake:\n  catalog:\n    type: sqlite\n")  # the default path
    code, out = run(capsys, "validate", str(tmp_path))
    assert code == 0, out


@pytest.mark.parametrize("host", ["127.0.0.2", "'/tmp,db.example.com'", "localhost,127.0.0.2"])
@pytest.mark.parametrize("mode", ["", "    ssl_mode: require\n", "    ssl_mode: verify-ca\n"])
def test_a_remote_postgres_catalog_needs_verify_full(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    host: str,
) -> None:
    # A host list counts as remote when any entry is (libpq tries them in turn).
    monkeypatch.setenv("LAKE_TEST_PASSWORD", "secret-value")
    project(
        tmp_path,
        f"lake:\n  catalog:\n    type: postgres\n    host: {host}\n    database: d\n"
        f"    user: u\n    password: ${{env:LAKE_TEST_PASSWORD}}\n{mode}",
    )
    out = problem(capsys, tmp_path)
    assert out.startswith("LAKE_TLS_REQUIRED project.yaml")
    assert "secret-value" not in out


@pytest.mark.parametrize(
    "catalog",
    [
        "    type: mysql\n    host: h\n",
        "    type: quack\n    path: x\n",
        "    type: postgres\n    host: localhost\n    database: d\n    user: u\n"
        "    password: literal-secret\n",
    ],
)
def test_unsupported_catalogs_and_literal_passwords_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], catalog: str
) -> None:
    project(tmp_path, f"lake:\n  catalog:\n{catalog}")
    out = problem(capsys, tmp_path)
    assert out.startswith("PROJECT_SCHEMA_INVALID project.yaml")
    assert "literal-secret" not in out


def test_an_unset_password_variable_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LAKE_TEST_UNSET", raising=False)
    project(
        tmp_path,
        "lake:\n  catalog:\n    type: postgres\n    host: localhost\n    database: d\n"
        "    user: u\n    password: ${env:LAKE_TEST_UNSET}\n",
    )
    out = problem(capsys, tmp_path)
    assert out.startswith("PROJECT_SECRET_MISSING project.yaml")
    assert "LAKE_TEST_UNSET" in out


def test_an_unset_certificate_variable_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKE_TEST_PASSWORD", "secret-value")
    monkeypatch.delenv("LAKE_TEST_CA", raising=False)
    project(
        tmp_path,
        "lake:\n  catalog:\n    type: postgres\n    host: db.example.com\n    database: d\n"
        "    user: u\n    password: ${env:LAKE_TEST_PASSWORD}\n    ssl_mode: verify-full\n"
        "    ssl_root_cert: ${env:LAKE_TEST_CA}\n",
    )
    out = problem(capsys, tmp_path)
    assert out.startswith("PROJECT_SECRET_MISSING")
    assert "LAKE_TEST_CA" in out


def test_values_cannot_add_connection_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKE_TEST_PASSWORD", "secret-value")
    project(
        tmp_path,
        "lake:\n  catalog:\n    type: postgres\n    host: localhost\n"
        '    database: "d sslmode=disable host=evil.example.com"\n'
        '    user: "it\'s"\n    password: ${env:LAKE_TEST_PASSWORD}\n',
    )
    place = lake_place.place(load_project(tmp_path), home(tmp_path))
    connection = place.location.connection  # type: ignore[union-attr]
    assert connection == (
        "host='localhost' port='5432' dbname='d sslmode=disable host=evil.example.com' "
        "user='it\\'s'"
    )


# --- flows on every catalog (CI) -------------------------------------------------------------


def test_no_lake_section_keeps_the_lake_where_it_was(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path)
    full_flow(tmp_path, capsys)
    assert (home(tmp_path) / "lake" / "catalog.ducklake").is_file()
    assert not (home(tmp_path) / lake_place.RECORD_FILE).exists()


def test_a_duckdb_catalog_at_another_local_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "catalogs" / "billing.ducklake"
    project(tmp_path, f"lake:\n  catalog:\n    type: duckdb\n    path: {catalog}\n")
    full_flow(tmp_path, capsys)
    assert catalog.is_file()
    assert not (home(tmp_path) / "lake" / "catalog.ducklake").exists()


def test_a_sqlite_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, "lake:\n  catalog:\n    type: sqlite\n    path: catalog/lake.sqlite\n")
    full_flow(tmp_path, capsys)
    catalog = tmp_path / "catalog" / "lake.sqlite"
    assert catalog.read_bytes()[:16] == b"SQLite format 3\x00"
    assert not (home(tmp_path) / "lake" / "catalog.ducklake").exists()


def postgres_service(variable: str = "STORE_TEST_POSTGRES") -> dict[str, str]:
    connection = os.environ.get(variable)
    if not connection or not os.environ.get(PASSWORD_ENV):
        if os.environ.get("CI"):
            pytest.fail(f"{variable} and {PASSWORD_ENV} are required in CI")
        pytest.skip("no PostgreSQL configured for this run")
    return dict(pair.split("=", 1) for pair in connection.split())


def postgres_lake(service: dict[str, str], schema: str, extra: str = "") -> str:
    return (
        f"lake:\n  catalog:\n    type: postgres\n    host: {service['host']}\n"
        f"    port: {service['port']}\n    database: {service['dbname']}\n"
        f"    user: {service['user']}\n    password: ${{env:{PASSWORD_ENV}}}\n"
        f"    schema: {schema}\n{extra}"
    )


def catalog_tables(service: dict[str, str], schema: str) -> int:
    connection = duckdb.connect()
    try:
        connection.execute(
            f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD '{os.environ[PASSWORD_ENV]}')"
        )
        connection.execute(
            f"ATTACH 'host={service['host']} port={service['port']} dbname={service['dbname']} "
            f"user={service['user']}' AS pg (TYPE postgres, READ_ONLY)"
        )
        row = connection.execute(
            "SELECT count(*) FROM pg.information_schema.tables WHERE table_schema = ? "
            "AND table_name = 'ducklake_snapshot'",
            [schema],
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def test_a_postgres_catalog_on_the_local_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = postgres_service()
    schema = f"lake_{uuid.uuid4().hex[:12]}"
    project(tmp_path, postgres_lake(service, schema))
    full_flow(tmp_path, capsys)
    assert catalog_tables(service, schema) == 1
    assert not (home(tmp_path) / "lake" / "catalog.ducklake").exists()
    assert (home(tmp_path) / lake_place.RECORD_FILE).is_file()


def test_a_remote_postgres_catalog_over_verified_tls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = postgres_service("STORE_TEST_POSTGRES_TLS")
    authority = os.environ.get("STORE_TEST_POSTGRES_TLS_CA")
    assert authority, "STORE_TEST_POSTGRES_TLS_CA is required with STORE_TEST_POSTGRES_TLS"
    monkeypatch.setenv("LAKE_TEST_CA", authority)
    assert service["host"] not in {"localhost", "127.0.0.1", "::1"}  # remote for the rule
    schema = f"lake_{uuid.uuid4().hex[:12]}"
    project(
        tmp_path,
        postgres_lake(
            service, schema, "    ssl_mode: verify-full\n    ssl_root_cert: ${env:LAKE_TEST_CA}\n"
        ),
    )
    full_flow(tmp_path, capsys)


def test_verify_full_against_a_server_without_tls_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = postgres_service()
    postgres_service("STORE_TEST_POSTGRES_TLS")  # the CA below is the TLS service's
    monkeypatch.setenv("LAKE_TEST_CA", os.environ["STORE_TEST_POSTGRES_TLS_CA"])
    remote = {**service, "host": "127.0.0.2"}
    project(
        tmp_path,
        postgres_lake(
            remote,
            "lake_never",
            "    ssl_mode: verify-full\n    ssl_root_cert: ${env:LAKE_TEST_CA}\n",
        ),
    )
    feed(tmp_path, 3)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 1
    assert "STORE_CATALOG_UNAVAILABLE" in out
    assert os.environ[PASSWORD_ENV] not in out
    assert not (home(tmp_path) / lake_place.RECORD_FILE).exists()


def test_switching_catalogs_after_the_lake_exists_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, "lake:\n  catalog:\n    type: duckdb\n")
    feed(tmp_path, 5)
    assert run(capsys, "run", str(tmp_path))[0] == 0
    catalog = home(tmp_path) / "lake" / "catalog.ducklake"
    before = catalog.read_bytes()
    project(tmp_path, "lake:\n  catalog:\n    type: sqlite\n")
    out = problem(capsys, tmp_path)
    assert out.startswith("LAKE_LOCATION_CHANGED project.yaml")
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 1
    assert "LAKE_LOCATION_CHANGED" in out
    assert catalog.read_bytes() == before  # the existing lake is untouched
    assert not (home(tmp_path) / "lake" / "catalog.sqlite").exists()
    project(tmp_path)  # back to the default: the same lake again
    assert run(capsys, "run", str(tmp_path))[0] == 0
    assert count(tmp_path) == 5


# --- one identity however the folder is named ------------------------------------------------


def bare(folder: Path, lake: str) -> Path:
    """A project with no models: validating it opens no lake."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.yaml").write_text(PROJECT + lake, encoding="utf-8")
    return folder


def problems(folder: Path) -> list[str]:
    return [str(problem) for problem in commands.check_project(folder).problems]


def test_a_dotted_folder_name_is_the_same_lake(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", "lake:\n  catalog:\n    type: duckdb\n")
    (tmp_path / "other").mkdir()
    default = home(folder) / "lake" / "catalog.ducklake"
    default.parent.mkdir(parents=True)
    default.touch()  # an existing default lake
    assert problems(tmp_path / "other" / ".." / "proj") == []
    assert problems(folder) == []


def test_a_symlinked_folder_is_the_same_lake(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", "lake:\n  catalog:\n    type: sqlite\n    path: c/l.sqlite\n")
    (tmp_path / "link").symlink_to(folder)
    linked = tmp_path / "link"
    chosen = lake_place.place(load_project(linked), home(linked))
    lake_place.record(home(linked), chosen)  # first opened through the link
    assert problems(folder) == []
    assert problems(linked) == []
    assert chosen.identity["path"] == (folder / "c" / "l.sqlite").resolve().as_posix()


def test_the_default_path_is_recognized_through_a_dotted_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lake_place, "file_system", lambda path: None)
    bare(tmp_path / "proj", "lake:\n  catalog:\n    type: sqlite\n")
    (tmp_path / "other").mkdir()
    assert problems(tmp_path / "other" / ".." / "proj") == []
