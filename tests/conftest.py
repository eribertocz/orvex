"""The repository-level tests' one setting: an engine the security executor cannot serve is a
skip on a developer's machine and a failure in CI (`tests/support/environment.py`, #427)."""

from __future__ import annotations

from conformance.runners import security_engines
from tests.support.environment import unavailable

security_engines.on_unavailable = unavailable
