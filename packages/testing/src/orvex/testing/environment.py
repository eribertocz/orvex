"""A test whose environment is missing: skipped on a developer's machine, failed in CI (#427).

A mandatory guarantee never turns green in CI by not running: CI provides every service the
suite needs, so a missing one there is a broken run, not a reason to skip."""

from __future__ import annotations

import os
from typing import NoReturn

import pytest


def unavailable(skip_reason: str, needed: str) -> NoReturn:
    """Skip with `skip_reason` locally; fail in CI, naming what is `needed`."""
    if os.environ.get("CI"):
        pytest.fail(f"{needed} (missing in CI: {skip_reason})")
    pytest.skip(skip_reason)
