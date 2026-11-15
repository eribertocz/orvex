"""The server tests' shared fixture: every access instance a test opens is closed after it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .access_instance import close_opened


@pytest.fixture(autouse=True)
def _close_access_instances() -> Iterator[None]:
    yield
    close_opened()
