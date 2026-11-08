"""The lock's versions for the Windows job (#376)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci.pins import pins
from tools.repository import REPOSITORY_ROOT


def test_the_lock_versions_are_printed_and_a_missing_package_fails(tmp_path: Path) -> None:
    lock = tmp_path / "pylock.toml"
    lock.write_text('[[packages]]\nname = "duckdb"\nversion = "2.0.0"\n', "utf-8")
    assert pins(lock, ["duckdb"]) == ["duckdb==2.0.0"]
    with pytest.raises(SystemExit, match="pytest"):
        pins(lock, ["pytest"])


def test_the_windows_job_packages_are_in_the_lock() -> None:
    names = ["pytest", "pluggy", "iniconfig", "packaging", "pygments", "duckdb"]
    assert len(pins(REPOSITORY_ROOT / "pylock.toml", names)) == len(names)
