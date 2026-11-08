"""The instance's protected set and existing lakes kept apart from it, both ways, across
projects (P2-30 PR 2, #399; ADR-0027 §3, "Separate locations")."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.server import lake_separation
from orvex.server.commands import check_project
from orvex.server.identity import load_identity
from orvex.server.instance import ProjectRegistry
from orvex.server.problems import Problem
from orvex.store.errors import StoreError

SLUG = load_identity().slug
PROJECT = "version: 1\nname: {name}\ntime_zone: UTC\n"


def lake(name: str, data: Path | str, schema: str = "billing", host: str = "localhost") -> str:
    return (
        f"  {name}:\n    type: ducklake\n    catalog:\n      type: postgres\n"
        f"      host: {host}\n      port: 5432\n      database: lakes\n"
        f"      user: {name}_reader\n      password: ${{env:LAKE_PASSWORD}}\n"
        f"      schema: {schema}\n    data_path: {data}\n    read_only: true\n"
    )


def make_project(folder: Path, *entries: str, settings: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.yaml").write_text(PROJECT.format(name=folder.name) + settings)
    if entries:
        (folder / "connections.yaml").write_text("version: 1\nconnections:\n" + "".join(entries))
    return folder


def problems_of(instance: Path, folder: Path, order: list[Path]) -> list[Problem]:
    for registered in order:
        ProjectRegistry(instance).register(registered)
    found = lake_separation.instance(instance, lake_separation.registered(instance, folder), SLUG)
    return lake_separation.problems(found, folder)


@pytest.fixture
def other_device(tmp_path: Path) -> Iterator[Path]:
    """A folder on another file system than `tmp_path` (`/dev/shm`), where an existing lake's
    data can live apart from the projects."""
    shared = Path("/dev/shm")
    if not shared.is_dir() or shared.stat().st_dev == tmp_path.stat().st_dev:
        if os.environ.get("CI"):
            pytest.fail("CI needs /dev/shm on a file system of its own")
        pytest.skip("no second file system here")
    folder = Path(tempfile.mkdtemp(dir=shared, prefix="server-test-"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    return tmp_path / "instance"


def overlap_case(case: str, tmp_path: Path, shared: Path) -> tuple[Path, Path]:
    """Project A has an existing lake at `shared/lakes/`; project B declares, in each case, a
    protected location inside it."""
    lakes = shared / "lakes"
    lakes.mkdir()
    first = make_project(tmp_path / "a", lake("billing", f"{lakes}/"))
    if case == "managed lake data":
        second = make_project(
            tmp_path / "b", settings=f"lake:\n  data:\n    path: {lakes / 'managed-b'}\n"
        )
    elif case == "file source":
        second = make_project(
            tmp_path / "b", f"  readings:\n    type: parquet\n    path: {lakes}/x/*.parquet\n"
        )
    elif case == "project folder":
        second = make_project(lakes / "b")
    else:  # another existing lake, with another catalog
        second = make_project(tmp_path / "b", lake("collections", f"{lakes}/inner/", "coll"))
    return first, second


@pytest.mark.parametrize("case", ["managed lake data", "file source", "project folder", "lake"])
@pytest.mark.parametrize("registered_first", ["a", "b"])
@pytest.mark.parametrize("validated", ["a", "b"])
def test_each_overlap_is_refused_in_both_orders_across_two_projects(
    tmp_path: Path,
    other_device: Path,
    instance: Path,
    case: str,
    registered_first: str,
    validated: str,
) -> None:
    first, second = overlap_case(case, tmp_path, other_device)
    order = [first, second] if registered_first == "a" else [second, first]
    found = problems_of(instance, first if validated == "a" else second, order)
    assert [problem.code for problem in found] == ["STORE_SOURCE_INVALID"]
    assert "overlaps a protected location" in found[0].message
    if validated == "a":
        # Another project's paths are never named to this one.
        assert str(second) not in found[0].message
        assert found[0].file == "connections.yaml"


def test_a_lake_location_containing_the_managed_lakes_data_of_its_own_project_is_refused(
    tmp_path: Path, other_device: Path, instance: Path
) -> None:
    managed = other_device / "lakes" / "managed"
    managed.mkdir(parents=True)
    folder = make_project(
        tmp_path / "a",
        lake("billing", f"{other_device / 'lakes'}/"),
        settings=f"lake:\n  data:\n    path: {managed}\n",
    )
    found = problems_of(instance, folder, [folder])
    assert [problem.code for problem in found] == ["STORE_SOURCE_INVALID"]
    assert "the managed lake's data location" in found[0].message


def test_the_same_lake_connected_in_two_projects_is_accepted(
    tmp_path: Path, other_device: Path, instance: Path
) -> None:
    data = f"{other_device / 'shared'}/"
    (other_device / "shared").mkdir()
    first = make_project(tmp_path / "a", lake("billing", data))
    # The same catalog identity (host case aside) and data location, spelled with a dot.
    second = make_project(
        tmp_path / "b", lake("billing", f"{other_device}/./shared", host="LOCALHOST")
    )
    for folder in (first, second):
        assert problems_of(instance, folder, [first, second]) == []


def test_one_data_location_under_two_catalogs_is_two_lakes_and_refused(
    tmp_path: Path, other_device: Path, instance: Path
) -> None:
    data = f"{other_device / 'shared'}/"
    first = make_project(tmp_path / "a", lake("billing", data))
    second = make_project(tmp_path / "b", lake("billing", data, schema="other"))
    for folder in (first, second):
        found = problems_of(instance, folder, [first, second])
        assert [problem.code for problem in found] == ["STORE_SOURCE_INVALID"]


def test_a_local_lake_on_the_file_system_of_a_project_is_refused(
    tmp_path: Path, instance: Path
) -> None:
    # No overlap by path, but the data shares the projects' file system: a hard link there
    # could reach a protected file.
    (tmp_path / "lake-data").mkdir()
    folder = make_project(tmp_path / "a", lake("billing", f"{tmp_path / 'lake-data'}/"))
    found = problems_of(instance, folder, [folder])
    assert [problem.code for problem in found] == ["STORE_SOURCE_INVALID"]
    assert "file system that holds a protected location" in found[0].message


def test_an_object_storage_lake_apart_from_the_projects_is_accepted(
    tmp_path: Path, instance: Path
) -> None:
    s3 = (
        "    s3:\n      region: us-east-1\n      key_id: ${env:BILLING_KEY}\n"
        "      secret: ${env:BILLING_SECRET}\n"
    )
    folder = make_project(tmp_path / "a", lake("billing", "s3://lakes/billing/") + s3)
    assert problems_of(instance, folder, [folder]) == []


def test_a_project_that_cannot_be_read_refuses_the_lakes_of_the_others(
    tmp_path: Path, other_device: Path, instance: Path
) -> None:
    (other_device / "data").mkdir()
    folder = make_project(tmp_path / "a", lake("billing", f"{other_device / 'data'}/"))
    broken = make_project(tmp_path / "b")
    (broken / "project.yaml").write_text("version: 1\nname: [unclosed\n")
    found = problems_of(instance, folder, [folder, broken])
    assert [problem.code for problem in found] == ["STORE_SOURCE_INVALID"]
    assert "cannot be read" in found[0].message
    plain = make_project(tmp_path / "c")
    assert problems_of(instance, plain, [folder, broken, plain]) == []
    instance_set = lake_separation.instance(instance, [folder, broken], SLUG)
    with pytest.raises(StoreError):
        lake_separation.protection(instance_set, folder, "billing")


def test_the_protection_of_a_lake_covers_the_whole_instance(
    tmp_path: Path, other_device: Path, instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MANAGED", "not read")
    data = f"{other_device / 'shared'}/"
    source = tmp_path / "sources" / "readings.sqlite"
    first = make_project(
        tmp_path / "a",
        lake("billing", data),
        "  ledger:\n    type: postgres\n    host: localhost\n    port: 5432\n"
        "    database: erp\n    user: ledger_reader\n    password: ${env:LEDGER}\n"
        "    read_only: true\n",
        settings="lake:\n  catalog:\n    type: sqlite\n",
    )
    second = make_project(
        tmp_path / "b",
        lake("billing", data),
        f"  meters:\n    type: sqlite\n    path: {source}\n    read_only: true\n",
        lake("collections", f"{other_device / 'coll'}/", "coll"),
        settings=(
            "lake:\n  catalog:\n    type: postgres\n    host: localhost\n    port: 5432\n"
            "    database: managed\n    user: managed_b\n    password: ${env:MANAGED}\n"
        ),
    )
    found = lake_separation.instance(instance, [first, second], SLUG)
    protection = lake_separation.protection(found, first, "billing")
    roles = {item.root for item in protection.roles_in_use}
    # Every other connection's role and the managed catalogs' roles; never its own.
    assert roles == {"ledger_reader", "billing_reader", "collections_reader", "managed_b"}
    files = {item.root for item in protection.protected_files}
    locations = {item.root for item in protection.protected_locations}
    home = first.resolve() / f".{SLUG}"
    assert {str(home / "lake" / "catalog.sqlite"), str(source)} <= files
    assert str(instance.resolve() / "server.sqlite") in files
    assert files <= locations
    assert {
        str(instance.resolve()),
        str(first.resolve()),
        str(second.resolve()),
        f"{other_device / 'coll'}/",
        str(home / "lake" / "data") + "/",
    } <= locations
    # The same lake connected in project b is this lake, not a protected location.
    assert data not in locations


def test_validation_with_the_instance_reports_an_overlap(
    tmp_path: Path, other_device: Path, instance: Path
) -> None:
    first, second = overlap_case("file source", tmp_path, other_device)
    for folder in (first, second):
        ProjectRegistry(instance).register(folder)
    checked = check_project(second, instance=instance)
    assert [problem.code for problem in checked.problems] == ["STORE_SOURCE_INVALID"]
    assert check_project(second).problems == ()
