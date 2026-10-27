from pathlib import Path

import pytest
from sextant.server.identity import load_identity
from sextant.server.instance import (
    ProjectPathError,
    ProjectRegistry,
    home_variable,
    in_memory_project,
    instance_directory,
    open_project,
)

IDENTITY = load_identity()


def test_home_variable_is_derived_from_the_slug() -> None:
    assert home_variable(IDENTITY) == f"{IDENTITY.slug.upper().replace('-', '_')}_HOME"


def test_instance_directory_override(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    assert instance_directory(IDENTITY, {home_variable(IDENTITY): str(target)}) == target
    assert target.is_dir()


def test_default_instance_directory_is_hidden_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert instance_directory(IDENTITY, {}) == tmp_path / f".{IDENTITY.slug}"


def test_open_creates_registers_and_reopens(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "instance")
    folder = tmp_path / "billing"
    first = open_project(folder, registry)
    assert folder.is_dir()
    again = open_project(folder, registry)
    assert again.id == first.id
    assert again.last_opened >= first.last_opened
    assert [project.path for project in registry.projects()] == [folder.resolve()]


def test_file_is_not_a_project(tmp_path: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("x")
    with pytest.raises(ProjectPathError, match="PROJECT_PATH_NOT_A_DIRECTORY"):
        open_project(file, ProjectRegistry(tmp_path / "instance"))


def test_in_memory_project_leaves_nothing_behind() -> None:
    with in_memory_project(IDENTITY) as project:
        assert project.in_memory
        assert project.path.is_dir()
        folder = project.path
    assert not folder.exists()
