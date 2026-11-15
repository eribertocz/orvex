"""A command's call context holds real values: the operating-system user, the project folder,
and a revision that names the project's files as they are now."""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

import pytest
from orvex.contracts.interface_errors import ContractCallRefused
from orvex.server import command_caller
from orvex.server.command_caller import files_digest, project_context, project_revision


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


FILES = {"project.yaml": "version: 1\n", "models/s/a.sql": "SELECT 1 AS id\n"}


def git(folder: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t.invalid", *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_without_git_the_revision_is_the_files_digest(tmp_path: Path) -> None:
    write(tmp_path, FILES)
    before = project_revision(tmp_path)
    assert before == f"files:{files_digest(tmp_path)}"
    write(tmp_path, {"models/s/a.sql": "SELECT 2 AS id\n"})
    assert project_revision(tmp_path) != before
    write(tmp_path, {"notes.txt": "not read by a check\n"})
    assert project_revision(tmp_path) == f"files:{files_digest(tmp_path)}"


def test_a_clean_checkout_is_its_commit_and_a_changed_one_adds_its_files(tmp_path: Path) -> None:
    write(tmp_path, FILES)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "models")
    commit = git(tmp_path, "rev-parse", "HEAD")
    assert project_revision(tmp_path) == f"git:{commit}"
    write(tmp_path, {"models/s/a.sql": "SELECT 3 AS id\n"})
    assert project_revision(tmp_path) == f"git:{commit}+files:{files_digest(tmp_path)}"


def test_the_context_is_the_user_the_folder_and_the_revision(tmp_path: Path) -> None:
    write(tmp_path, FILES)
    context = project_context(tmp_path)
    assert context.actor == getpass.getuser()
    assert context.project_id == str(tmp_path)
    assert context.revision == project_revision(tmp_path)
    assert context.correlation_id != project_context(tmp_path).correlation_id


def test_an_actor_that_cannot_be_found_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_caller.getpass, "getuser", lambda: "unknown")
    with pytest.raises(ContractCallRefused) as raised:
        project_context(tmp_path)
    assert raised.value.code == "CONTRACT_CONTEXT_INVALID"
