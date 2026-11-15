"""Which implementation an instance chooses per interface contract (#486 §2.2), read from its
configuration; anything misread stops the server."""

from __future__ import annotations

from pathlib import Path

import pytest
from orvex.server.assembly import AssemblyRefused
from orvex.server.assembly_settings import assembly_choices
from orvex.server.identity import load_identity

IDENTITY = load_identity()
VARIABLE = f"{IDENTITY.slug.upper().replace('-', '_')}_ASSEMBLY_CONFIG"


def test_no_file_means_no_choice(tmp_path: Path) -> None:
    assert assembly_choices(IDENTITY, tmp_path, {}) == {}


def test_the_instance_file_and_the_variable_are_read(tmp_path: Path) -> None:
    text = '[implementations]\nresult-cache = "alternatives.result_cache_ipc"\n'
    (tmp_path / "assembly.toml").write_text(text)
    assert assembly_choices(IDENTITY, tmp_path, {}) == {
        "result-cache": "alternatives.result_cache_ipc"
    }
    elsewhere = tmp_path / "other.toml"
    elsewhere.write_text('[implementations]\nstore = "store"\n')
    assert assembly_choices(IDENTITY, tmp_path, {VARIABLE: str(elsewhere)}) == {"store": "store"}


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        '[choices]\nstore = "store"\n',
        "implementations = 1\n",
        '[implementations]\nResult_Cache = "store"\n',
        '[implementations]\nstore = "Store-Module"\n',
        "[implementations]\nstore = 1\n",
    ],
    ids=["unreadable", "other table", "not a table", "contract name", "module path", "not text"],
)
def test_a_misread_configuration_stops_the_server(tmp_path: Path, text: str) -> None:
    (tmp_path / "assembly.toml").write_text(text)
    with pytest.raises(AssemblyRefused) as raised:
        assembly_choices(IDENTITY, tmp_path, {})
    assert [p.split(":", 1)[0] for p in raised.value.problems] == ["ASSEMBLY_CONFIG_INVALID"]


def test_a_directory_in_place_of_the_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "assembly.toml").mkdir()
    with pytest.raises(AssemblyRefused):
        assembly_choices(IDENTITY, tmp_path, {})
