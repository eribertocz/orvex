"""The conformance tests' one setting: an engine the security executor cannot serve is a
skip on a developer's machine and a failure in CI (`tests/support/environment.py`, #427).

The executor is imported inside the fixture, once a test runs: imported while pytest loads
its first conftest files, its modules would be read before coverage starts measuring them.
"""

from __future__ import annotations

import pytest
from orvex.testing.environment import unavailable


@pytest.fixture(autouse=True)
def _engines_unavailable_as_this_run_says(monkeypatch: pytest.MonkeyPatch) -> None:
    from conformance.runners import security_engines  # imported here: see the docstring

    monkeypatch.setattr(security_engines, "on_unavailable", unavailable)
