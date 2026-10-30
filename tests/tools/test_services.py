"""#173: a test service is optional locally, required in CI."""

from __future__ import annotations

import pytest

from tests.support.services import required_in_ci


def test_a_service_variable_is_returned_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_TEST_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CI", "true")
    assert required_in_ci("SERVICE_TEST_URL") == "http://127.0.0.1:1"


def test_a_missing_service_skips_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVICE_TEST_URL", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert required_in_ci("SERVICE_TEST_URL") is None


def test_a_missing_service_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVICE_TEST_URL", raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="SERVICE_TEST_URL is required in CI"):
        required_in_ci("SERVICE_TEST_URL")
