"""The module manifest `module.toml` (#486 §2.5): provides, requires and declared dependencies."""

from __future__ import annotations

from importlib.resources import files

import orvex.contracts
import pytest
from orvex.contracts.interface import Version
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.manifest import Provided, Required, parse_manifest

FULL = """
module = "store"

[dependencies]
runtime = ["duckdb", "PyArrow", "orvex-contracts"]
tests = ["pytest"]

[[provides]]
contract = "store"
version = "1.2"
capabilities = ["read", "write"]
implementation = "Store"

[[requires]]
contract = "security-context"
version = "1.0"
capabilities = ["read"]
"""


def test_a_full_manifest_is_read() -> None:
    manifest = parse_manifest(FULL, "store")
    assert manifest.module == "store"
    assert manifest.provides == (Provided("store", Version(1, 2), ("read", "write"), "Store"),)
    assert manifest.requires == (Required("security-context", Version(1, 0), ("read",)),)
    assert manifest.runtime == ("duckdb", "orvex-contracts", "pyarrow")
    assert manifest.tests == ("pytest",)


def test_the_shipped_manifest_of_the_contracts_distribution_is_valid() -> None:
    text = files(orvex.contracts).joinpath("module.toml").read_text("utf-8")
    manifest = parse_manifest(text, "contracts")
    assert manifest.module == "contracts"
    assert manifest.provides == ()
    assert manifest.requires == ()
    assert manifest.runtime == ("pydantic",)


def test_provides_and_requires_may_be_absent() -> None:
    manifest = parse_manifest('module = "mcp"\n[dependencies]\nruntime = []\ntests = []\n', "mcp")
    assert (manifest.provides, manifest.requires, manifest.runtime) == ((), (), ())


INVALID = {
    "not toml": "module = ",
    "no module": "[dependencies]\nruntime = []\ntests = []\n",
    "bad module": 'module = "Store"\n[dependencies]\nruntime = []\ntests = []\n',
    "unknown key": FULL + 'extra = "x"\n',
    "unknown provides key": FULL.replace(
        'implementation = "Store"', 'implementation = "Store"\nskip = ["read"]'
    ),
    "no dependencies": 'module = "store"\n',
    "unknown dependency kind": FULL.replace('tests = ["pytest"]', 'tests = ["pytest"]\ndev = []'),
    "bad distribution": FULL.replace('"duckdb"', '"duck db"'),
    "duplicated distribution": FULL.replace('"duckdb"', '"duckdb", "DuckDB"'),
    "bad version": FULL.replace('"1.2"', '"1"'),
    "no capabilities": FULL.replace('["read", "write"]', "[]"),
    "duplicated capability": FULL.replace('["read", "write"]', '["read", "read"]'),
    "bad capability": FULL.replace('["read", "write"]', '["Read"]'),
    "dotted implementation": FULL.replace('"Store"', '"internal.Store"'),
    "module path implementation": FULL.replace('"Store"', '"orvex.store.lake:Store"'),
    "provides twice": FULL
    + '[[provides]]\ncontract = "store"\nversion = "1.0"\ncapabilities = ["read"]\n'
    + 'implementation = "Other"\n',
    "requires twice": FULL
    + '[[requires]]\ncontract = "security-context"\nversion = "1.0"\ncapabilities = ["read"]\n',
    "bad contract": FULL.replace('contract = "store"', 'contract = "Store"'),
    "not a string list": FULL.replace('tests = ["pytest"]', "tests = [1]"),
}


@pytest.mark.parametrize("text", INVALID.values(), ids=INVALID.keys())
def test_an_invalid_manifest_is_refused(text: str) -> None:
    with pytest.raises(InterfaceContractError) as raised:
        parse_manifest(text, "store")
    assert raised.value.code == "MODULE_MANIFEST_INVALID"
    assert raised.value.details["module"] == "store"
