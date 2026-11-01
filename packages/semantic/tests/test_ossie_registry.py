"""P2-03 (#278, ADR-0030 §1): the version registry and the packaged schemas."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from importlib.resources import as_file, files
from pathlib import Path
from typing import cast

import pytest
import yaml
from orvex.semantic.ossie import extensions
from orvex.semantic.ossie.reader import read
from orvex.semantic.ossie.registry import Registry, RegistryInvalid, default_registry, load_entry

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "vendor" / "apache-ossie"
PACKAGED = files("orvex.semantic.ossie").joinpath("revisions")
DOCUMENT = yaml.safe_dump(
    {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [
            {"name": "d", "source": "d", "fields": [{"name": "f", "expression": {"sql": "f"}}]}
        ],
    }
).encode()


def test_the_registry_has_one_stable_entry_for_the_pin() -> None:
    (entry,) = default_registry().entries
    assert (entry.id, entry.state, entry.specification_version) == (
        "c721ffc",
        "stable",
        "0.2.0.dev0",
    )
    assert {"DUCKDB", "CLICKHOUSE", "X"} <= entry.dialects(["X"])


def test_the_packaged_entry_is_byte_identical_to_vendor() -> None:
    folder = PACKAGED.joinpath("c721ffc")
    for name in ("PIN.toml", "ossie-schema.json"):
        assert folder.joinpath(name).read_bytes() == (VENDOR / name).read_bytes(), name


def test_every_vendored_file_matches_the_registry() -> None:
    (entry,) = default_registry().entries
    for name, digest in entry.sha256.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_one_changed_byte_in_a_packaged_schema_fails_the_hash(tmp_path: Path) -> None:
    with as_file(PACKAGED.joinpath("c721ffc")) as source:
        copy = tmp_path / "c721ffc"
        shutil.copytree(source, copy)
    schema = copy / "ossie-schema.json"
    data = bytearray(schema.read_bytes())
    data[-2] ^= 0x01
    schema.write_bytes(bytes(data))
    with pytest.raises(RegistryInvalid, match="SHA-256"):
        load_entry(copy)


def test_the_packaged_extension_schema_is_the_contract() -> None:
    contract = ROOT / "contracts" / "ossie-extensions" / "v1" / "schema.json"
    assert (
        hashlib.sha256(extensions.schema_bytes()).digest()
        == hashlib.sha256(contract.read_bytes()).digest()
    )


def test_one_changed_byte_in_the_extension_schema_fails_the_hash() -> None:
    contract = (ROOT / "contracts" / "ossie-extensions" / "v1" / "schema.json").read_bytes()
    changed = bytearray(extensions.schema_bytes())
    changed[-2] ^= 0x01
    assert hashlib.sha256(bytes(changed)).digest() != hashlib.sha256(contract).digest()


@pytest.mark.parametrize(
    ("pattern", "text", "matches"),
    [
        ("^[^\\s.]+$", "sale", True),
        ("^[^\\s.]+$", "sale\n", False),
        ("^P(?!$)([0-9]+D)?$", "P", False),
        ("^[$]+$", "$$", True),
        ("^a\\$$", "a$", True),
    ],
)
def test_patterns_are_full_matches(pattern: str, text: str, matches: bool) -> None:
    import re

    assert bool(re.search(extensions.full_match(pattern), text)) is matches


def test_two_active_entries_for_one_number_are_refused() -> None:
    entry = default_registry().entries[0]
    other = dataclasses.replace(entry, id="other", commit="0" * 40)
    with pytest.raises(RegistryInvalid, match="both active"):
        Registry((entry, other))
    with pytest.raises(RegistryInvalid, match="two registry entries"):
        Registry((entry, dataclasses.replace(entry, state="rejected")))


def test_with_one_entry_inactive_the_active_one_reads_and_is_named() -> None:
    entry = default_registry().entries[0]
    retired = dataclasses.replace(entry, id="older", commit="0" * 40, state="rejected")
    registry = Registry((retired, entry))
    assert read(DOCUMENT, "m.ossie.yaml", ["X"], registry=registry).registry_entry == "c721ffc"
    newer = dataclasses.replace(entry, id="newer", commit="1" * 40)
    registry = Registry((dataclasses.replace(entry, state="rejected"), newer))
    assert read(DOCUMENT, "m.ossie.yaml", ["X"], registry=registry).registry_entry == "newer"


def test_a_read_only_entry_loads_for_preservation_only() -> None:
    entry = dataclasses.replace(default_registry().entries[0], state="read_only")
    registry = Registry(
        (entry, dataclasses.replace(entry, id="kept", state="stable", specification_version="9"))
    )
    loaded = read(DOCUMENT, "m.ossie.yaml", ["X"], registry=registry)
    assert loaded.preservation_only
    assert loaded.registry_state == "read_only"


def test_an_unknown_standard_member_is_a_schema_problem() -> None:
    document = json.loads(json.dumps(yaml.safe_load(DOCUMENT)))
    document["datasets"][0]["fields"][0]["semantics"] = {}
    from orvex.semantic import SemanticError

    with pytest.raises(SemanticError) as caught:
        read(json.dumps(document).encode(), "m.ossie.json", ["X"])
    (problem,) = cast(list[dict[str, str]], caught.value.details["problems"])
    assert (problem["rule"], problem["pointer"]) == ("schema", "/datasets/0/fields/0")
