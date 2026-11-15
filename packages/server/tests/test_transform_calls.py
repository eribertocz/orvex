"""The server's calls to transform go through its contract, each with the envelope of the
command it belongs to, and none is sent outside a command (#486 §2 item 6)."""

from __future__ import annotations

import getpass
from pathlib import Path

import pytest
from orvex.server.command_caller import project_revision
from orvex.server.transform_calls import CallContextMissing, calling, envelope, transform


def test_no_envelope_is_made_outside_a_command() -> None:
    with pytest.raises(CallContextMissing) as raised:
        envelope("lineage_project_key")
    assert raised.value.code == "CALL_CONTEXT_MISSING"


def test_a_call_inside_a_command_carries_its_real_context(tmp_path: Path) -> None:
    (tmp_path / "project.yaml").write_text("version: 1\n", encoding="utf-8")
    with calling(tmp_path):
        sent = envelope("lineage_project_key")
        assert transform().lineage_project_key(sent, "Meters") == "meters"
    assert sent.actor == getpass.getuser()
    assert sent.project_id == str(tmp_path)
    assert sent.revision == project_revision(tmp_path)
    assert (sent.contract_name, sent.contract_version) == ("transform", "1.0")
    assert sent.idempotency_key is None


def test_one_command_keeps_one_context_per_project(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with calling(tmp_path):
        outer = envelope("closure")
        with calling(tmp_path / "."):
            assert envelope("closure") == outer
        with calling(other):
            inner = envelope("closure")
        assert envelope("closure") == outer
    assert inner.project_id == str(other)
    assert inner.correlation_id != outer.correlation_id
    with calling(tmp_path):
        assert envelope("closure").correlation_id != outer.correlation_id


def test_only_the_contract_operations_can_be_called() -> None:
    with pytest.raises(AttributeError):
        getattr(transform(), "run_everything")  # noqa: B009
