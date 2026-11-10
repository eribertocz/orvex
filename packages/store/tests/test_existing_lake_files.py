"""An existing DuckLake kept apart from the instance's other files (P2-30 PR 2, #399; ADR-0027
§3, "No links of any kind" and "Separate locations").

Every lake here has a SQLite catalog in WAL mode and its data files written as Parquet (no
inlined rows), so each snapshot references real files. The owner's catalog is edited directly,
as an owner able to write it could.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    CredentialsInUseItem,
    ExistingLakeAttachment,
    ObjectStorage,
    ProtectedFile,
    ProtectedLocation,
    S3Storage,
    Snapshot,
    SqliteLakeLocation,
)
from orvex.store import separation
from orvex.store.errors import StoreError
from orvex.store.existing_lake import ExistingLake

from tests.support.environment import unavailable

MEMORY = "512MB"


def make_lake(root: Path) -> tuple[Path, Path]:
    """A lake at `root`: `catalog.sqlite` and `data/`, with table `main.readings` written as
    one data file and one delete file."""
    catalog, data = root / "catalog.sqlite", root / "data"
    data.mkdir(parents=True)
    writer = duckdb.connect()
    for extension in ("ducklake", "sqlite"):
        writer.install_extension(extension)
        writer.load_extension(extension)
    writer.execute(
        f"ATTACH 'ducklake:sqlite:{catalog}' AS l "
        f"(DATA_PATH '{data.as_posix()}/', DATA_INLINING_ROW_LIMIT 0)"
    )
    writer.execute("CREATE TABLE l.readings (reading_id BIGINT)")
    writer.execute("INSERT INTO l.readings SELECT range FROM range(5)")
    writer.execute("DELETE FROM l.readings WHERE reading_id = 1")
    writer.close()
    connection = sqlite3.connect(catalog)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.close()
    return catalog, data


def edit_catalog(catalog: Path, sql: str, *parameters: object) -> None:
    connection = sqlite3.connect(catalog)
    with connection:
        connection.execute(sql, parameters)
    connection.close()


def attachment(
    catalog: Path,
    data: Path,
    locations: Sequence[Path | str] = (),
    files: Sequence[Path] = (),
) -> ExistingLakeAttachment:
    return ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name="billing",
        location=SqliteLakeLocation(
            kind="lake_location",
            catalog="sqlite",
            path=str(catalog),
            data_path=data.as_posix() + "/",
        ),
        roles_in_use=[],
        protected_files=[ProtectedFile(str(path)) for path in files],
        protected_locations=[ProtectedLocation(str(path)) for path in locations],
    )


def open_lake(made: ExistingLakeAttachment) -> ExistingLake:
    return ExistingLake(made, memory_limit=MEMORY, threads=1)


def refused_at_attach(made: ExistingLakeAttachment) -> StoreError:
    with pytest.raises(StoreError) as caught:
        open_lake(made).close()
    assert caught.value.code == "STORE_SOURCE_INVALID"
    return caught.value


def refused_at_verify(lake: ExistingLake, snapshot: Snapshot | None = None) -> StoreError:
    with pytest.raises(StoreError) as caught:
        lake.verify(snapshot or lake.latest_snapshot(), lake.attachment)
    assert caught.value.code == "STORE_SOURCE_INVALID"
    return caught.value


def files_of(lake: ExistingLake) -> tuple[Path, Path]:
    row = lake._connection.execute(
        "SELECT data_file, delete_file FROM ducklake_list_files('lake', 'readings')"
    ).fetchone()
    assert row is not None
    return Path(row[0]), Path(row[1])


def outside_parquet(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "elsewhere.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT range * 100 AS reading_id FROM range(5)) TO '{path}' (FORMAT parquet)"
    )
    return path


@pytest.fixture
def lake_root(other_device: Path) -> Path:
    """The lake lives on a file system of its own; the protected set in `tmp_path`."""
    return other_device / "owner"


@pytest.fixture
def protected(tmp_path: Path) -> tuple[list[Path], list[Path]]:
    """A protected set as the server builds it: a project folder, its managed lake's catalog
    file and data folder, and a Route A source file."""
    project = tmp_path / "project"
    managed = tmp_path / "managed"
    (managed / "data").mkdir(parents=True)
    project.mkdir()
    catalog = managed / "catalog.ducklake"
    catalog.write_bytes(b"managed")
    source = tmp_path / "sources" / "readings.sqlite"
    source.parent.mkdir()
    source.write_bytes(b"source")
    return [project, managed / "data", catalog, source], [catalog, source]


# --- a clean lake ----------------------------------------------------------------------------


def test_a_lake_apart_from_the_protected_set_attaches_and_its_snapshot_is_verified(
    lake_root: Path, protected: tuple[list[Path], list[Path]]
) -> None:
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data, *protected)) as lake:
        snapshot = lake.latest_snapshot()
        assert lake.verify(snapshot, lake.attachment) == snapshot
        found = lake._connection.execute("SELECT sum(reading_id) FROM lake.readings").fetchone()
        assert found == (9,)


def test_an_attachment_without_the_protected_locations_is_refused(lake_root: Path) -> None:
    catalog, data = make_lake(lake_root)
    made = attachment(catalog, data).model_copy(update={"protected_locations": None})
    assert "protected locations" in refused_at_attach(made).message


# --- separate locations ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("/srv/a", "/srv/a/", True),
        ("/srv/a", "/srv/a/b", True),
        ("/srv/a/b", "/srv", True),
        ("/srv/a", "/srv/ab", False),
        ("/srv/a/../b", "/srv/b/c", True),
        ("s3://lakes/billing/", "s3://lakes/billing/2024/", True),
        ("s3://lakes/billing/", "s3://lakes/", True),
        ("s3://lakes/billing/", "s3://lakes/billing2/", False),
        # One store can be reached through another scheme: the bucket and key decide.
        ("s3://lakes/billing/", "gs://lakes/billing/x/", True),
        ("az://lakes/billing/", "s3://other/billing/", False),
        ("az://lakes/billing/", "az://lakes/billing/2024/", True),
        ("s3://lakes/billing/", "/lakes/billing", False),
    ],
)
def test_locations_overlap_by_whole_components_after_resolving(
    first: str, second: str, expected: bool
) -> None:
    assert separation.overlaps("billing", first, second) is expected
    assert separation.overlaps("billing", second, first) is expected


@pytest.mark.parametrize(
    "text",
    [
        "s3://lakes/a/../b/",
        "s3://lakes//b/",
        "s3://",
        "relative/path",
        "http://x/y",
        # The same container as az://managed/data/, spelled with its account (Reviewer pass 1).
        "az://acct.blob.core.windows.net/managed/data/",
        "/srv/lake\x00/x",
    ],
)
def test_a_location_that_cannot_be_compared_is_refused(text: str) -> None:
    with pytest.raises(StoreError) as caught:
        separation.overlaps("billing", "/srv/lake", text)
    assert caught.value.code == "STORE_SOURCE_INVALID"


def test_a_link_is_resolved_before_locations_are_compared(tmp_path: Path) -> None:
    real = tmp_path / "managed" / "data"
    real.mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "managed")
    assert separation.overlaps("billing", str(tmp_path / "alias"), str(real))


@pytest.mark.parametrize("where", ["data inside", "data contains", "catalog inside"])
def test_a_lake_overlapping_a_protected_location_is_refused_in_both_orders(
    other_device: Path, where: str
) -> None:
    root = other_device / "lakes"
    catalog, data = make_lake(root / "owner")
    protected = {
        "data inside": root / "owner",  # a protected folder holds the data location
        "data contains": data / "main",  # a protected location lies inside the data location
        "catalog inside": root,
    }[where]
    error = refused_at_attach(attachment(catalog, data, [protected]))
    assert "overlaps a protected location" in error.message or "catalog lies" in error.message


# --- file systems apart ----------------------------------------------------------------------


def test_a_local_data_location_on_a_file_system_holding_a_protected_location_is_refused(
    tmp_path: Path, other_device: Path
) -> None:
    # The catalog is apart; the data folder shares `tmp_path`'s file system with a managed
    # lake's catalog, so a hard link there could reach it.
    catalog, _ = make_lake(other_device / "owner")
    data = tmp_path / "lake-data"
    shutil.copytree(other_device / "owner" / "data", data)
    edit_catalog(
        catalog, "UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'", f"{data}/"
    )
    managed = tmp_path / "managed" / "catalog.ducklake"
    managed.parent.mkdir()
    managed.write_bytes(b"managed")
    for locations, files in (([managed], [managed]), ([managed.parent], [])):
        error = refused_at_attach(attachment(catalog, data, locations, files))
        assert "file system that holds a protected location" in error.message
    # A protected location not created yet counts by the folder it would be created in.
    error = refused_at_attach(attachment(catalog, data, [tmp_path / "later" / "lake"]))
    assert "file system" in error.message


def test_a_sqlite_catalog_on_a_file_system_holding_a_protected_location_is_refused(
    tmp_path: Path, other_device: Path
) -> None:
    # SQLite opens the -wal and -shm files itself, outside the descriptor checks: only a file
    # system of its own keeps a hard link to a protected file out of them (finding 4 on #405).
    _, data = make_lake(other_device / "owner")
    catalog = tmp_path / "catalog.sqlite"
    shutil.copy(other_device / "owner" / "catalog.sqlite", catalog)
    project = tmp_path / "project"
    project.mkdir()
    error = refused_at_attach(attachment(catalog, data, [project]))
    assert "SQLite catalog is on a file system" in error.message


def test_the_devices_under_a_location_include_every_mount_below_it(tmp_path: Path) -> None:
    table = separation.parse_mounts(
        "22 1 254:0 / / rw - ext4 /dev/vda rw\n"
        f"30 22 0:44 / {tmp_path}/project/mnt rw - xfs /dev/vdb rw\n"
        f"31 22 0:45 / {tmp_path}/elsewhere rw - xfs /dev/vdc rw\n"
        f"32 22 0:46 / {str(tmp_path).replace(' ', chr(92) + '040')}/sp\\040ace rw - xfs x rw\n"
    )
    (tmp_path / "project").mkdir()
    devices = separation._devices_under("billing", str(tmp_path / "project"), table)
    assert os.makedev(0, 44) in devices
    assert os.makedev(0, 45) not in devices
    assert (os.makedev(0, 46), tmp_path / "sp ace", "xfs") in table


def test_a_data_folder_its_mount_does_not_identify_is_refused(other_device: Path) -> None:
    # A nested subvolume or an overlay shows another device than its mount: ambiguous.
    folder = other_device / "data"
    folder.mkdir()
    table = [
        (os.makedev(9, 9), Path("/"), "ext4"),
        (os.makedev(9, 8), other_device.parent, "tmpfs"),
    ]
    with pytest.raises(StoreError, match="cannot be identified"):
        separation.own_device("billing", str(folder), table)


# --- the files of a snapshot -----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["data", "delete"])
@pytest.mark.parametrize("target", ["outside", "route a source", "another project"])
def test_a_snapshot_referencing_a_file_outside_the_location_is_not_served(
    lake_root: Path, tmp_path: Path, kind: str, target: str
) -> None:
    # The catalog registers another file by its absolute path: one outside, a Route A
    # source's, or another project's managed-lake data file (both protected).
    catalog, data = make_lake(lake_root)
    folder = {"outside": "elsewhere", "route a source": "sources", "another project": "proj/lake"}
    path = outside_parquet(tmp_path / folder[target])
    protected = [] if target == "outside" else [path.parent]
    table = "ducklake_data_file" if kind == "data" else "ducklake_delete_file"
    with open_lake(attachment(catalog, data, protected)) as lake:
        snapshot = lake.latest_snapshot()
        lake.verify(snapshot, lake.attachment)
        edit_catalog(catalog, f"UPDATE {table} SET path = ?, path_is_relative = 0", str(path))
        assert "outside its data location" in refused_at_verify(lake, snapshot).message


def test_a_snapshot_referencing_a_file_by_a_dot_dot_path_is_not_served(lake_root: Path) -> None:
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        edit_catalog(catalog, "UPDATE ducklake_data_file SET path = '../../../catalog.sqlite-wal'")
        assert "ambiguous path" in refused_at_verify(lake).message


@pytest.mark.parametrize("link", ["file", "table folder", "schema folder"])
def test_a_snapshot_reaching_a_file_through_a_symbolic_link_is_not_served(
    lake_root: Path, other_device: Path, link: str
) -> None:
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        data_file, _ = files_of(lake)
        if link == "file":
            data_file.unlink()
            data_file.symlink_to(outside_parquet(other_device / "elsewhere"))
        else:
            folder = data_file.parent if link == "table folder" else data_file.parent.parent
            moved = other_device / "moved"
            shutil.move(folder, moved)
            folder.symlink_to(moved)
        assert "symbolic link" in refused_at_verify(lake).message


def test_a_symbolic_link_swapped_in_after_verification_is_refused_at_read(
    lake_root: Path, other_device: Path
) -> None:
    # The read-time rule: DuckDB 2.0.0 resolves a link before it checks the allow-list, so a
    # data file swapped for a link out of the location after verification is never read
    # (measured for #399, PR 2).
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        lake.verify(lake.latest_snapshot(), lake.attachment)
        data_file, _ = files_of(lake)
        data_file.unlink()
        data_file.symlink_to(outside_parquet(other_device / "elsewhere"))
        # A column is read (DuckLake answers `count(*)` from its statistics, opening no file).
        with pytest.raises(duckdb.PermissionException):
            lake._connection.execute("SELECT sum(reading_id) FROM lake.readings").fetchall()


def test_a_snapshot_referencing_a_missing_file_or_a_folder_is_not_served(lake_root: Path) -> None:
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        _, delete_file = files_of(lake)
        delete_file.unlink()
        assert "does not exist" in refused_at_verify(lake).message
        delete_file.mkdir()
        assert "not a regular file" in refused_at_verify(lake).message


def test_a_hard_link_to_a_protected_file_is_not_served(lake_root: Path) -> None:
    # A hard link cannot leave the file system, which holds no protected file once the lake is
    # attached; the file check still compares every file with the protected files.
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        data_file, _ = files_of(lake)
        status = data_file.stat()
        with pytest.raises(StoreError, match="hard link to a protected file"):
            separation.check_files(
                "billing",
                f"{data}/",
                [str(data_file)],
                (os.stat(data).st_dev, os.stat(data).st_ino),
                frozenset({(status.st_dev, status.st_ino)}),
            )


def test_a_hard_link_to_a_managed_lake_file_is_refused_with_its_file_system(
    tmp_path: Path, other_device: Path
) -> None:
    catalog, _ = make_lake(other_device / "owner")
    data = tmp_path / "lake-data"
    shutil.copytree(other_device / "owner" / "data", data)
    edit_catalog(
        catalog, "UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'", f"{data}/"
    )
    managed = tmp_path / "managed" / "catalog.ducklake"
    managed.parent.mkdir()
    managed.write_bytes(b"managed")
    data_file = next(data.rglob("ducklake-*[0-9a-f].parquet"))
    data_file.unlink()
    os.link(managed, data_file)
    error = refused_at_attach(attachment(catalog, data, [managed], [managed]))
    assert "hard link" in error.message


def test_object_storage_files_must_lie_under_the_prefix() -> None:
    prefix = "s3://lakes/billing/"
    separation.check_files("billing", prefix, [f"{prefix}main/t/a.parquet"], None, frozenset())
    for path in ("s3://lakes/other/a.parquet", f"{prefix}main/../../x.parquet", f"{prefix}m//a"):
        with pytest.raises(StoreError):
            separation.check_files("billing", prefix, [path], None, frozenset())


def test_a_snapshot_whose_commit_time_changed_is_not_served(lake_root: Path) -> None:
    # A catalog restored with reused ids: the id alone never identifies a snapshot.
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        snapshot = lake.latest_snapshot()
        moved = snapshot.model_copy(
            update={"committed_at": snapshot.committed_at + dt.timedelta(microseconds=1)}
        )
        assert "commit time" in refused_at_verify(lake, moved).message


def test_the_protected_set_is_checked_again_as_it_stands_at_verification(
    lake_root: Path, tmp_path: Path, other_device: Path
) -> None:
    # A protected location named through a link: at attach it leads to another file system;
    # by the next verification it leads to the lake's own.
    catalog, data = make_lake(lake_root)
    alias = tmp_path / "managed-data"
    (tmp_path / "elsewhere").mkdir()
    alias.symlink_to(tmp_path / "elsewhere")
    with open_lake(attachment(catalog, data, [alias])) as lake:
        lake.verify(lake.latest_snapshot(), lake.attachment)
        alias.unlink()
        (other_device / "now-here").mkdir()
        alias.symlink_to(other_device / "now-here")
        assert "file system" in refused_at_verify(lake).message


def test_a_data_folder_replaced_after_the_attach_is_not_served(
    lake_root: Path, other_device: Path
) -> None:
    catalog, data = make_lake(lake_root)
    with open_lake(attachment(catalog, data)) as lake:
        shutil.move(data, other_device / "old-data")
        shutil.copytree(other_device / "old-data", data)
        assert "replaced" in refused_at_verify(lake).message


def test_sqlite_refuses_companion_files_that_are_symbolic_links(tmp_path: Path) -> None:
    # Measured on DuckDB 2.0.0's SQLite: a catalog whose -wal or -shm is a link is not opened
    # and the link's target is left untouched; with the file-system rule this closes the window
    # between the descriptor checks and SQLite's own open (finding 4 on #405).
    for suffix in ("-wal", "-shm"):
        folder = tmp_path / suffix
        folder.mkdir()
        catalog = folder / "catalog.sqlite"
        connection = sqlite3.connect(catalog)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE x (a)")
        connection.commit()
        connection.close()
        target = folder / "target"
        target.write_bytes(b"protected")
        Path(f"{catalog}{suffix}").symlink_to(target)
        reader = duckdb.connect()
        reader.load_extension("sqlite")
        reader.execute(f"ATTACH '{catalog}' AS c (TYPE sqlite, READ_ONLY)")
        with pytest.raises(duckdb.Error, match="unable to open"):
            reader.execute("SELECT * FROM c.x").fetchall()
        reader.close()
        assert target.read_bytes() == b"protected"


# --- Reviewer pass 1 on #407 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["overlay", "nfs4", "cifs", "9p", "virtiofs", "fuse.sshfs", "btrfs"]
)
def test_a_file_system_whose_devices_do_not_tell_disks_apart_is_refused_on_every_side(
    tmp_path: Path, kind: str
) -> None:
    # Overlay shows its own device for a file of a lower layer; a network or FUSE file system
    # lets the server link files; btrfs gives subvolumes devices of their own. Refused for the
    # data folder, the SQLite catalog and every protected location.
    folder = tmp_path / "data"
    folder.mkdir()
    device = folder.stat().st_dev
    stacked = [(device, Path("/"), kind)]
    with pytest.raises(StoreError, match=kind):
        separation.own_device("billing", str(folder), stacked)
    with pytest.raises(StoreError, match=kind):
        separation._devices_under("billing", str(folder), stacked)
    below = [(device, Path("/"), "ext4"), (device + 1, folder / "mnt", kind)]
    with pytest.raises(StoreError, match=kind):
        separation._devices_under("billing", str(folder), below)
    assert separation.own_device("billing", str(folder), [(device, Path("/"), "ext4")]) == device


def test_the_mount_table_gives_each_mounts_type() -> None:
    table = separation.parse_mounts(
        "25 24 0:23 / /dev/shm rw,relatime - tmpfs tmpfs rw\n"
        "30 22 0:44 / /var/lib/docker/overlay2/x/merged rw shared:1 - overlay overlay rw\n"
    )
    assert [kind for _, _, kind in table] == ["tmpfs", "overlay"]


OVERLAY = """
import json, os, subprocess, sys
from pathlib import Path
from orvex.store import separation
from orvex.store.errors import StoreError
root = Path(sys.argv[1])
for name in ("lower", "upper", "work", "merged", "project"):
    (root / name).mkdir()
subprocess.run(["mount", "-t", "overlay", "overlay", "-o",
    f"lowerdir={root}/lower,upperdir={root}/upper,workdir={root}/work", str(root / "merged")],
    check=True)
(root / "merged" / "data").mkdir()
(root / "project" / "secret.parquet").write_bytes(b"protected")
found = {}
for side, data, protected in (
    ("data on overlay", root / "merged" / "data", root / "project"),
    ("protected on overlay", root / "project", root / "merged" / "data"),
):
    try:
        separation.check_apart("billing", f"{data}/", None, [str(protected)])
        found[side] = "accepted"
    except StoreError as error:
        found[side] = error.details["reason"]
print(json.dumps(found))
"""


def test_an_overlay_file_system_is_refused_for_local_data_in_both_directions(
    tmp_path: Path,
) -> None:
    # Measured by the Reviewer: through an overlay, a hard link made in the upper layer to a
    # protected file passes any device comparison. Mounted in a mount namespace of its own.
    if shutil.which("unshare") is None:
        unavailable("no unshare here", "unshare, for the overlay hard-link test")
    python = [
        "env",
        f"PYTHONPATH={os.pathsep.join(sys.path)}",
        sys.executable,
        "-c",
        OVERLAY,
        str(tmp_path),
    ]
    # A mount namespace of its own: as root; else as an unprivileged user namespace; else,
    # where those are not allowed (GitHub's Ubuntu runners), through passwordless sudo.
    if os.geteuid() == 0:
        attempts = [["unshare", "-m"]]
    else:
        attempts = [["unshare", "-rm"], ["sudo", "-n", "unshare", "-m"]]
    result = None
    for command in attempts:
        result = subprocess.run(
            [*command, *python], capture_output=True, text=True, check=False, timeout=120
        )
        if result.returncode == 0:
            break
    assert result is not None
    if result.returncode != 0 and not os.environ.get("CI"):
        pytest.skip(f"no mount namespace allowed here: {result.stderr.strip()[:200]}")
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert all("overlay" in reason for reason in found.values()), found


def s3_attachment(
    in_use: list[str] | None, key: str = "LAKE_KEY", secret: str = "LAKE_SECRET"
) -> ExistingLakeAttachment:
    return ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name="billing",
        location=SqliteLakeLocation(
            kind="lake_location",
            catalog="sqlite",
            path="/srv/lakes/billing.sqlite",
            data_path="s3://lakes/billing/",
            storage=ObjectStorage(
                S3Storage(
                    type="s3", region="us-east-1", use_ssl=True, key_id_env=key, secret_env=secret
                )
            ),
        ),
        roles_in_use=[],
        protected_files=[],
        protected_locations=[],
        credentials_in_use=None if in_use is None else [CredentialsInUseItem(v) for v in in_use],
    )


@pytest.mark.parametrize(
    ("in_use", "reason"),
    [
        (None, "credentials in use were not given"),
        (["LAKE_KEY"], "another connection's variable"),
        (["MANAGED_KEY"], "another connection's"),  # the same key id under another name
        (["MANAGED_SECRET"], "another connection's"),
    ],
)
def test_an_object_storage_lake_needs_a_credential_of_its_own(
    monkeypatch: pytest.MonkeyPatch, in_use: list[str] | None, reason: str
) -> None:
    monkeypatch.setenv("LAKE_KEY", "AKIALAKE")
    monkeypatch.setenv("LAKE_SECRET", "lake-secret")
    monkeypatch.setenv("MANAGED_KEY", "AKIALAKE")
    monkeypatch.setenv("MANAGED_SECRET", "lake-secret")
    error = refused_at_attach(s3_attachment(in_use))
    assert reason in error.message
    assert "AKIALAKE" not in error.message
    assert "lake-secret" not in error.message


def test_a_credential_check_compares_azure_account_keys_and_passes_a_credential_of_its_own() -> (
    None
):
    lake = "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=a2V5;EndpointSuffix=x"
    other = "AccountName=a;AccountKey=a2V5;DefaultEndpointsProtocol=https"
    with pytest.raises(StoreError, match="another connection's"):
        separation.check_credentials("billing", {"AZ_LAKE": lake}, ["AZ"], {"AZ": other})
    separation.check_credentials(
        "billing", {"K": "AKIALAKE", "S": "s"}, ["M"], {"M": "AKIAMANAGED"}
    )
    with pytest.raises(StoreError, match="no credential of its own"):
        separation.check_credentials("billing", {"AZ_LAKE": ""}, [], {})


S3_SERVER = """
import duckdb, http.server, json, socketserver, sys, threading
from tests.support.environment import unavailable
hits = []
class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self):
        hits.append([self.path, bool(self.headers.get("Authorization"))])
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()
    do_GET = do_HEAD = answer
    def log_message(self, *arguments):
        pass
server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]
c = duckdb.connect()
c.load_extension("httpfs")
c.execute(
    "CREATE TEMPORARY SECRET (TYPE s3, KEY_ID 'k', SECRET 's', REGION 'us-east-1', "
    f"ENDPOINT '127.0.0.1:{port}', USE_SSL false, URL_STYLE 'path', SCOPE 's3://lakebucket/lake/')"
)
for statement in (
    "SET allowed_directories = ['s3://lakebucket/lake/']",
    "SET enable_external_access = false",
    "SET lock_configuration = true",
):
    c.execute(statement)
for url in ("s3://lakebucket/lake/../../otherbucket/x.parquet", "s3://otherbucket/x.parquet"):
    try:
        c.execute(f"SELECT * FROM read_parquet('{url}')").fetchall()
    except duckdb.Error as error:
        hits.append([url, type(error).__name__])
print(json.dumps(hits))
sys.stdout.flush()
"""


def test_duckdb_resolves_dot_dot_in_object_storage_after_the_allow_list() -> None:
    # Measured on DuckDB 2.0.0 (Reviewer pass 1 on #407): an address under the allowed prefix
    # passes the check, then `..` is resolved and the request is signed with the lake's
    # credential for another bucket. So for object storage the boundary at read is the
    # credential, not the allow-list. This test fails once DuckDB checks the resolved address.
    result = subprocess.run(
        [sys.executable, "-c", S3_SERVER],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    if "httpfs" in result.stderr and "not found" in result.stderr.lower():
        unavailable("the httpfs extension is not installed here", "httpfs, for the S3 prefix test")
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert ["/otherbucket/x.parquet", True] in found  # signed, outside the prefix
    assert ["s3://otherbucket/x.parquet", "PermissionException"] in found  # as written: refused


def test_a_nul_character_in_a_path_is_refused_not_raised(lake_root: Path) -> None:
    _, data = make_lake(lake_root)
    with pytest.raises(StoreError):
        separation.check_apart("billing", f"{data}/", None, [], ["/srv/a\x00b"])
    with pytest.raises(StoreError):
        separation.check_apart("billing", f"{data}\x00/", None, [])
    with pytest.raises(StoreError):
        separation.check_files("billing", f"{data}/", [f"{data}/a\x00b"], (1, 1), frozenset())
