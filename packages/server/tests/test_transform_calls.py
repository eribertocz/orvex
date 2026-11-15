"""The server's calls to transform go through its contract with a complete envelope, and fail
closed without one (#486 §2 item 6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from orvex.contracts.interface_errors import ContractCallRefused
from orvex.server.transform_calls import ACTOR, REVISION, calling, envelope, transform


def test_a_call_outside_a_project_is_refused_before_transform_runs() -> None:
    with pytest.raises(ContractCallRefused) as raised:
        transform().lineage_project_key(envelope(), "Meters")
    assert raised.value.code == "CONTRACT_ENVELOPE_INVALID"
    assert raised.value.details["field"] == "correlation_id"


def test_a_call_inside_a_project_carries_its_envelope(tmp_path: Path) -> None:
    with calling(tmp_path):
        sent = envelope()
        assert transform().lineage_project_key(envelope(), "Meters") == "meters"
    assert (sent.project_id, sent.actor, sent.revision) == (str(tmp_path), ACTOR, REVISION)
    assert (sent.contract_name, sent.contract_version) == ("transform", "1.0")
    assert sent.correlation_id
    assert envelope().project_id == ""


def test_one_command_keeps_one_correlation_id_per_project(tmp_path: Path) -> None:
    with calling(tmp_path):
        outer = envelope()
        with calling(tmp_path / "."):
            assert envelope() == outer
        with calling(tmp_path / "other"):
            inner = envelope()
        assert envelope() == outer
    assert inner.project_id == str(tmp_path / "other")
    assert inner.correlation_id != outer.correlation_id
    with calling(tmp_path):
        assert envelope().correlation_id != outer.correlation_id


def test_only_the_contract_operations_can_be_called() -> None:
    with pytest.raises(AttributeError):
        getattr(transform(), "run_everything")  # noqa: B009
