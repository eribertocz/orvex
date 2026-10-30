"""#120: the Builder chooses where the lake's catalog lives (`lake.catalog` in `project.yaml`).

Validation tests run anywhere. The flows open real lakes (DuckLake), so they run where the
extensions are available (CI). The PostgreSQL flows need `STORE_TEST_POSTGRES` (+ password)
and, for TLS, `STORE_TEST_POSTGRES_TLS` and `STORE_TEST_POSTGRES_TLS_CA`; CI provides them.
"""

from __future__ import annotations

import io
import json
import os
import uuid
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import ReadRequest
from orvex.server import commands, lake_place
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.project import ProjectLoadError, load_project
from orvex.store.errors import StoreError
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
    assert not home(tmp_path).exists()  # nothing created (#146)


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
    assert not home(tmp_path).exists()  # nothing created (#146)


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
    catalog = home(tmp_path) / "lake" / "catalog.ducklake"
    assert catalog.is_file()
    # The default lake is recorded too (#204), with the same place as before.
    assert record_text(tmp_path) == {"catalog": "duckdb", "path": catalog.resolve().as_posix()}


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
    # A PostgreSQL catalog cannot be shown to be absent: its record stays unfinished (#146).
    assert record_text(tmp_path)["state"] == "creating"


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
    lake_place.begin(home(linked), chosen)  # first opened through the link
    lake_place.confirm(home(linked), chosen)
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


# --- #146: the record is written before the first open and confirmed after it -------------------

SQLITE = "lake:\n  catalog:\n    type: sqlite\n    path: c/l.sqlite\n"
OTHER_SQLITE = "lake:\n  catalog:\n    type: sqlite\n    path: other/l.sqlite\n"
POSTGRES = (
    "lake:\n  catalog:\n    type: postgres\n    host: {host}\n    port: {port}\n"
    "    database: d\n    user: u\n    password: ${{env:LAKE_TEST_PASSWORD}}\n"
)


def chosen(folder: Path) -> lake_place.Place:
    return lake_place.place(load_project(folder), home(folder))


def record_text(folder: Path) -> dict[str, object]:
    found: dict[str, object] = json.loads((home(folder) / lake_place.RECORD_FILE).read_text())
    return found


def test_a_first_open_that_never_started_leaves_nothing_binding(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    lake_place.begin(home(folder), chosen(folder))
    assert record_text(folder)["state"] == "creating"
    assert not lake_place.exists(home(folder))  # not a lake yet
    # No catalog file: nothing was created there, so other settings are accepted ...
    bare(folder, OTHER_SQLITE)
    assert problems(folder) == []
    # ... and replace the unfinished record.
    lake_place.begin(home(folder), chosen(folder))
    assert "other" in str(record_text(folder)["path"])


def test_an_unfinished_first_open_binds_once_its_catalog_exists(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    lake_place.begin(home(folder), chosen(folder))
    catalog = folder / "c" / "l.sqlite"
    catalog.parent.mkdir(parents=True)
    catalog.touch()  # the open created the catalog, then stopped before confirming
    assert problems(folder) == []  # the same settings: the run goes on and confirms
    bare(folder, OTHER_SQLITE)
    found = problems(folder)
    assert found[0].startswith("LAKE_LOCATION_CHANGED project.yaml"), found
    assert "did not finish" in found[0]
    assert f".{SLUG}/{lake_place.RECORD_FILE}" in found[0]


def test_confirm_makes_the_record_the_lakes(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    place = chosen(folder)
    lake_place.begin(home(folder), place)
    lake_place.confirm(home(folder), place)
    assert "state" not in record_text(folder)
    assert lake_place.exists(home(folder))
    bare(folder, OTHER_SQLITE)
    found = problems(folder)
    assert "was created with other settings" in found[0], found


def test_abandon_drops_the_record_only_when_nothing_was_created(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    place = chosen(folder)
    lake_place.begin(home(folder), place)
    lake_place.abandon(home(folder), place)
    assert not (home(folder) / lake_place.RECORD_FILE).exists()
    lake_place.begin(home(folder), place)
    (folder / "c").mkdir()
    (folder / "c" / "l.sqlite").touch()
    lake_place.abandon(home(folder), place)
    assert record_text(folder)["state"] == "creating"


def test_an_unfinished_postgres_record_binds_other_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A PostgreSQL catalog cannot be checked from here: the unfinished record stays binding.
    monkeypatch.setenv("LAKE_TEST_PASSWORD", "secret-value")
    folder = bare(tmp_path / "proj", POSTGRES.format(host="localhost", port=1))
    place = chosen(folder)
    lake_place.begin(home(folder), place)
    lake_place.abandon(home(folder), place)
    assert record_text(folder)["state"] == "creating"
    assert problems(folder) == []
    bare(folder, POSTGRES.format(host="localhost", port=2))
    found = problems(folder)
    assert "did not finish" in found[0], found
    assert "secret-value" not in found[0]


def test_a_postgres_host_is_compared_without_letter_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKE_TEST_PASSWORD", "secret-value")
    folder = bare(tmp_path / "proj", POSTGRES.format(host="localhost", port=5432))
    place = chosen(folder)
    lake_place.begin(home(folder), place)
    lake_place.confirm(home(folder), place)
    bare(folder, POSTGRES.format(host="LOCALHOST", port=5432))
    assert problems(folder) == []


@pytest.mark.parametrize(
    "text", ['{"catalog": "sqlite", "path": "/x", "state": "other"}', "[]", "{"]
)
def test_a_record_that_is_not_one_is_refused(tmp_path: Path, text: str) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    home(folder).mkdir(parents=True)
    (home(folder) / lake_place.RECORD_FILE).write_text(text)
    found = problems(folder)
    assert found[0].startswith(f"LAKE_LOCATION_CHANGED .{SLUG}/{lake_place.RECORD_FILE}"), found


def test_a_record_written_before_146_keeps_its_meaning(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    home(folder).mkdir(parents=True)
    identity = dict(chosen(folder).identity)
    (home(folder) / lake_place.RECORD_FILE).write_text(json.dumps(identity))
    assert lake_place.exists(home(folder))
    assert problems(folder) == []
    bare(folder, OTHER_SQLITE)
    assert "was created with other settings" in problems(folder)[0]


def test_a_stop_after_the_first_open_leaves_a_record_the_next_run_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # #146 / #147 item 2: the default catalog with lake.encrypted, stopped right after the
    # open, before the record was confirmed.
    project(tmp_path, "lake:\n  encrypted: true\n")
    feed(tmp_path, 3)

    def stopped(home_: Path, place: lake_place.Place) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(lake_place, "confirm", stopped)
    with pytest.raises(KeyboardInterrupt):
        main(["run", str(tmp_path)])
    capsys.readouterr()
    assert record_text(tmp_path)["state"] == "creating"
    monkeypatch.undo()
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "LAKE_LOCATION_CHANGED" not in out
    assert "state" not in record_text(tmp_path)


def test_a_stop_after_a_sqlite_first_open_binds_its_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project(tmp_path, SQLITE)
    feed(tmp_path, 3)

    def stopped(home_: Path, place: lake_place.Place) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(lake_place, "confirm", stopped)
    with pytest.raises(KeyboardInterrupt):
        main(["run", str(tmp_path)])
    capsys.readouterr()
    monkeypatch.undo()
    project(tmp_path, SQLITE + "  data:\n    path: elsewhere\n")
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 1, out
    assert "did not finish" in out
    assert not (tmp_path / "elsewhere").exists()  # nothing opened


def test_a_sqlite_first_open_that_fails_leaves_no_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, SQLITE)
    feed(tmp_path, 3)
    (tmp_path / "c").write_text("a file where the catalog's folder goes")
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 1, out
    assert not (home(tmp_path) / lake_place.RECORD_FILE).exists()
    (tmp_path / "c").unlink()
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "state" not in record_text(tmp_path)


def test_schedule_on_a_sqlite_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from orvex.server.scheduler import schedule

    project(tmp_path, SQLITE)
    (tmp_path / "models" / "s" / "m.yaml").write_text(SIDECAR + "freshness: 5m\n")
    feed(tmp_path, 3)
    cycles: list[int] = []

    def sleep(seconds: float) -> None:
        cycles.append(1)

    assert (
        schedule(
            tmp_path,
            load_identity(),
            io.StringIO(),
            sleep=sleep,
            should_stop=lambda: len(cycles) > 0,
        )
        == 0
    )
    assert count(tmp_path) == 3
    assert "state" not in record_text(tmp_path)
    code, out = run(capsys, "runs", str(tmp_path))
    assert code == 0, out
    assert "success" in out


def default_lake(folder: Path) -> None:
    """The default DuckDB lake's catalog file, as its first open creates it."""
    catalog = home(folder) / "lake" / "catalog.ducklake"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.touch()


def test_an_unfinished_record_that_created_nothing_does_not_bind_a_default_lake(
    tmp_path: Path,
) -> None:
    # #181 Reviewer pass 1, reproduction A: stopped before the open with `encrypted: true`.
    folder = bare(tmp_path / "proj", "lake:\n  encrypted: true\n")
    lake_place.begin(home(folder), chosen(folder))
    assert record_text(folder)["state"] == "creating"
    bare(folder, "")  # no lake section: the default lake
    assert problems(folder) == []
    # Replaces the record that bound nothing with the default lake's own (#204).
    lake_place.begin(home(folder), chosen(folder))
    assert record_text(folder)["state"] == "creating"
    assert "encrypted" not in record_text(folder)
    default_lake(folder)  # the default lake is created, unencrypted
    lake_place.confirm(home(folder), chosen(folder))  # as its first open does (#204)
    assert problems(folder) == []
    bare(folder, "lake:\n  encrypted: true\n")
    found = problems(folder)
    assert "was created with other settings" in found[0], found


def test_a_default_lake_is_not_abandoned_for_an_unfinished_sqlite_record(tmp_path: Path) -> None:
    # #181 Reviewer pass 1, reproduction B.
    folder = bare(tmp_path / "proj", SQLITE)
    lake_place.begin(home(folder), chosen(folder))
    bare(folder, "")
    assert problems(folder) == []
    lake_place.begin(home(folder), chosen(folder))
    default_lake(folder)
    bare(folder, SQLITE)
    found = problems(folder)
    assert found[0].startswith("LAKE_LOCATION_CHANGED project.yaml"), found


def test_a_socket_folder_keeps_its_letter_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKE_TEST_PASSWORD", "secret-value")
    folder = bare(tmp_path / "proj", POSTGRES.format(host="/run/PG", port=5432))
    place = chosen(folder)
    lake_place.begin(home(folder), place)
    lake_place.confirm(home(folder), place)
    bare(folder, POSTGRES.format(host="/run/pg", port=5432))
    assert "was created with other settings" in problems(folder)[0]


def test_every_open_of_an_existing_lake_checks_again_under_the_lock(tmp_path: Path) -> None:
    # #181 Reviewer pass 2: `maintain`, `runs` and the scheduler's records passed the check
    # before another process created the lake with other settings; `open_existing` (their one
    # way in) checks again, so nothing is opened.
    folder = bare(tmp_path / "proj", OTHER_SQLITE)
    stale = chosen(folder)  # what `maintain` checked: no record yet
    bare(folder, SQLITE)
    created = chosen(folder)
    lake_place.begin(home(folder), created)  # another process's run creates its lake
    (folder / "c").mkdir()
    (folder / "c" / "l.sqlite").touch()
    lake_place.confirm(home(folder), created)
    with pytest.raises(ProjectLoadError) as raised:
        commands.open_existing(home(folder), stale)
    assert raised.value.code == "LAKE_LOCATION_CHANGED"
    assert not (folder / "other").exists()  # nothing opened or created


# --- a missing catalog file (#192) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("lake", "file"),
    [
        ("lake:\n  catalog:\n    type: duckdb\n    path: c/lake.ducklake\n", "c/lake.ducklake"),
        (SQLITE, "c/l.sqlite"),
    ],
    ids=["duckdb", "sqlite"],
)
def test_a_lake_whose_catalog_file_is_missing_is_refused_everywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lake: str, file: str
) -> None:
    from orvex.server import maintenance, scheduler

    project(tmp_path, lake)
    feed(tmp_path, 3)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    catalog = tmp_path / file
    kept = tmp_path / "kept"
    catalog.rename(kept)  # a deleted file, or a disk that is not mounted
    for command in ("run", "runs", "maintain"):
        code, out = run(capsys, command, str(tmp_path))
        assert code == 1, (command, out)
        assert "STORE_CATALOG_UNAVAILABLE" in out, (command, out)
        assert "Traceback" not in out
        assert not catalog.exists(), command  # no new, empty lake in its place
    out_buffer = io.StringIO()
    assert maintenance.history(tmp_path, load_identity(), 10, out_buffer) == 1
    assert "STORE_CATALOG_UNAVAILABLE" in out_buffer.getvalue()
    with pytest.raises(StoreError) as raised:
        scheduler._records(home(tmp_path), chosen(tmp_path))
    assert raised.value.code == "STORE_CATALOG_UNAVAILABLE"
    assert not catalog.exists()
    assert lake_place.exists(home(tmp_path))  # still a lake: reported, not "no lake yet"
    # Restored: the lake opens again with its history.
    kept.rename(catalog)
    feed(tmp_path, 5)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    code, out = run(capsys, "runs", str(tmp_path))
    assert code == 0, out
    assert out.count("success") == 2
    assert count(tmp_path) == 5


def test_a_creating_record_whose_file_is_missing_is_not_a_lake_yet(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", SQLITE)
    lake_place.begin(home(folder), chosen(folder))
    assert lake_place.missing_catalog(home(folder)) is None
    assert not lake_place.exists(home(folder))


@pytest.mark.parametrize("failing", [1, 2], ids=["begin", "confirm"])
def test_a_record_that_cannot_be_written_is_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failing: int,
) -> None:
    # #192: a full disk or a read-only folder while writing the record.
    project(tmp_path, SQLITE)
    feed(tmp_path, 3)
    calls: list[int] = []
    replace = os.replace

    def flaky(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        calls.append(1)
        if len(calls) == failing:
            raise OSError(28, "No space left on device")
        replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 1, out
    assert "LAKE_LOCATION_CHANGED" in out
    assert "cannot be written (OSError)" in out
    assert "Traceback" not in out
    assert not list(home(tmp_path).glob(".lake.location.*"))  # no half-written file
    monkeypatch.setattr(os, "replace", replace)
    # The lake was closed (or never opened): the next run opens it.
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert count(tmp_path) == 3


# --- the default lake is recorded (#204) ------------------------------------------------------


def test_a_default_lake_is_recorded_and_refused_when_its_catalog_is_lost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from orvex.server import scheduler

    project(tmp_path)  # no lake section: the default lake
    feed(tmp_path, 3)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    record = record_text(tmp_path)
    assert record["catalog"] == "duckdb"
    assert "state" not in record
    catalog = home(tmp_path) / "lake" / "catalog.ducklake"
    kept = tmp_path / "kept"
    catalog.rename(kept)
    for command in ("run", "runs", "maintain"):
        code, out = run(capsys, command, str(tmp_path))
        assert code == 1, (command, out)
        assert "STORE_CATALOG_UNAVAILABLE" in out, (command, out)
        assert not catalog.exists(), command  # no new, empty lake in its place
    with pytest.raises(StoreError) as raised:
        scheduler._records(home(tmp_path), chosen(tmp_path))
    assert raised.value.code == "STORE_CATALOG_UNAVAILABLE"
    kept.rename(catalog)
    feed(tmp_path, 5)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert count(tmp_path) == 5
    code, out = run(capsys, "runs", str(tmp_path))
    assert code == 0, out
    assert out.count("success") == 2  # the history before the loss is kept


@pytest.mark.parametrize("command", ["runs", "run"])
def test_a_default_lake_made_before_204_is_recorded_on_its_next_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    project(tmp_path)
    feed(tmp_path, 3)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    (home(tmp_path) / lake_place.RECORD_FILE).unlink()  # the layout before #204
    code, out = run(capsys, command, str(tmp_path))
    assert code == 0, out
    record = record_text(tmp_path)
    assert record["catalog"] == "duckdb"
    assert "state" not in record


def test_default_lake_data_without_catalog_or_record_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A default lake made before #204 that lost its catalog: its data files remain.
    project(tmp_path)
    feed(tmp_path, 3)
    code, out = run(capsys, "run", str(tmp_path))
    assert code == 0, out
    # Small writes are kept in the catalog (DuckLake inlines them); maintenance writes them out.
    code, out = run(capsys, "maintain", str(tmp_path))
    assert code == 0, out
    assert list((home(tmp_path) / "lake" / "data").rglob("*.parquet"))
    (home(tmp_path) / lake_place.RECORD_FILE).unlink()
    (home(tmp_path) / "lake" / "catalog.ducklake").unlink()
    assert lake_place.exists(home(tmp_path))
    for command in ("run", "runs"):
        code, out = run(capsys, command, str(tmp_path))
        assert code == 1, (command, out)
        assert "STORE_CATALOG_UNAVAILABLE" in out, (command, out)
        assert (home(tmp_path) / "lake" / "data").as_posix() in out, (command, out)
    assert not (home(tmp_path) / "lake" / "catalog.ducklake").exists()


def orphaned(folder: Path) -> Path:
    """A default lake made before #204: its catalog file and a data file, no record."""
    default_lake(folder)
    data = home(folder) / "lake" / "data" / "s" / "m"
    data.mkdir(parents=True)
    (data / "x.parquet").write_bytes(b"x")
    return home(folder) / "lake" / "catalog.ducklake"


def test_orphaned_data_is_named_in_the_refusal(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", "")
    orphaned(folder).unlink()
    with pytest.raises(StoreError) as raised:
        commands._refuse_missing_catalog(home(folder))
    data = (home(folder) / "lake" / "data").as_posix()
    assert raised.value.code == "STORE_CATALOG_UNAVAILABLE"
    assert data in str(raised.value)
    assert raised.value.details["data"] == data
    assert lake_place.RECORD_FILE not in str(raised.value)  # there is no record to remove


def test_a_failed_first_open_after_204_still_protects_the_default_lake(tmp_path: Path) -> None:
    # Reviewer pass 1 on #207: the first `run` after upgrading writes a `creating` record and
    # fails; a later `runs` confirms it, and a lost catalog is refused either way.
    folder = bare(tmp_path / "proj", "")
    catalog = orphaned(folder)
    place = chosen(folder)
    lake_place.begin(home(folder), place)
    lake_place.abandon(home(folder), place)  # the open failed; the catalog is there, so it stays
    assert record_text(folder)["state"] == "creating"
    catalog.unlink()  # lost before any other open
    assert lake_place.exists(home(folder))
    with pytest.raises(StoreError) as raised:
        commands._refuse_missing_catalog(home(folder))
    assert raised.value.code == "STORE_CATALOG_UNAVAILABLE"
    default_lake(folder)  # restored
    lake_place.adopt(home(folder), place)  # what `runs`, `maintain` and the scheduler do
    assert "state" not in record_text(folder)
    catalog.unlink()
    assert lake_place.missing_catalog(home(folder)) == catalog.as_posix()


def test_a_fresh_project_has_no_lake_and_no_orphan_data(tmp_path: Path) -> None:
    folder = bare(tmp_path / "proj", "")
    assert not lake_place.exists(home(folder))
    assert lake_place.missing_catalog(home(folder)) is None
    lake_place.adopt(home(folder), chosen(folder))  # no catalog: nothing to record
    assert not (home(folder) / lake_place.RECORD_FILE).exists()
