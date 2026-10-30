"""Test services that CI provides: a test that needs one skips where it is absent, but fails in
CI, so evidence for a phase's exit criteria cannot disappear without a failure (#173)."""

from __future__ import annotations

import os

import pytest


def required_in_ci(variable: str) -> str | None:
    """The variable's value; None where it is unset, except in CI, where that fails (at
    collection, when called at module level)."""
    value = os.environ.get(variable)
    if value is None and os.environ.get("CI"):
        pytest.fail(f"{variable} is required in CI", pytrace=False)
    return value
