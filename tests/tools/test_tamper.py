"""#429: the tampered copy of a lock differs from it by one hash digit only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci import tamper
from tools.repository import REPOSITORY_ROOT


def test_one_digit_of_the_first_hash_changes_and_nothing_else(tmp_path: Path) -> None:
    lock = (REPOSITORY_ROOT / "pylock.toml").read_text("utf-8")
    target = tmp_path / "pylock.tampered.toml"
    assert tamper.main([str(REPOSITORY_ROOT / "pylock.toml"), str(target)]) == 0
    changed = target.read_text("utf-8")
    differing = [i for i, (a, b) in enumerate(zip(lock, changed, strict=True)) if a != b]
    assert len(differing) == 1
    assert lock[differing[0] - 10 : differing[0]] == 'sha256 = "'


def test_a_name_pip_would_not_read_as_a_lock_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="pip reads a lock file only"):
        tamper.main([str(REPOSITORY_ROOT / "pylock.toml"), str(tmp_path / "tampered.toml")])
    with pytest.raises(SystemExit, match="no sha256"):
        tamper.tampered("[[packages]]\nname = 'x'\n")
