"""Finding the interface contracts, kits and public test adapters a contracts package ships, by
their place; every shipped adapter passes its contract's whole kit (#486 G4)."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import orvex.contracts
import pytest
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit_run import verify_kit
from orvex.contracts.registry import adapters, interface_contracts, kits

ADAPTER = """
from orvex.contracts.adapter import ContractAdapter
from .interface import CLOCK

class FixedClock:
    def now(self, envelope) -> int:
        return {answer}

CLOCK_ADAPTER = ContractAdapter(CLOCK, FixedClock)
"""

CONTRACT = """
from typing import Protocol
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.version import Version

class Error(Exception):
    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        super().__init__(code)

class Clock(Protocol):
    def now(self, envelope: CallEnvelope) -> int: ...

{name} = InterfaceContract(
    name="{contract}", version=Version(1, 0), protocol=Clock, error_type=Error,
    capabilities=("read",), errors=(), operations=(Operation("now", "read"),),
)
"""

KIT = """
from orvex.contracts.kit import Kit, KitCase
from .interface import CLOCK

def ticks(clock) -> None:
    if clock.now() != 7:
        raise AssertionError("the clock does not tick at 7")

CLOCK_KIT = Kit(CLOCK, recorded=1, cases=(KitCase("now.ticks", "now", "it answers", ticks),))
"""


@pytest.fixture
def package(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    name = f"probe_contracts_{request.node.name.split('[')[0][-30:]}"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    try:
        yield root
    finally:
        sys.path.remove(str(tmp_path))
        for module in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
            del sys.modules[module]


def write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_contracts_and_kits_are_found_in_their_places(package: Path) -> None:
    write(
        package,
        {
            "clock/__init__.py": "",
            "clock/interface.py": CONTRACT.format(name="CLOCK", contract="clock"),
            "clock/kit.py": KIT,
            "clock/adapter.py": ADAPTER.format(answer=7),
            "plain/__init__.py": "",
            "plain/v1.py": "X = 1\n",
        },
    )
    loaded = importlib.import_module(package.name)
    assert list(interface_contracts(loaded)) == ["clock"]
    assert list(kits(loaded)) == ["clock"]
    assert kits(loaded)["clock"].contract is interface_contracts(loaded)["clock"]
    assert list(adapters(loaded)) == ["clock"]
    assert verify_kit(kits(loaded)["clock"], adapters(loaded)["clock"].make)


def test_a_planted_adapter_that_fails_its_kit_is_caught(package: Path) -> None:
    write(
        package,
        {
            "clock/__init__.py": "",
            "clock/interface.py": CONTRACT.format(name="CLOCK", contract="clock"),
            "clock/kit.py": KIT,
            "clock/adapter.py": ADAPTER.format(answer=8),
        },
    )
    loaded = importlib.import_module(package.name)
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(kits(loaded)["clock"], adapters(loaded)["clock"].make)
    assert raised.value.code == "KIT_FAILED"


def test_two_contracts_with_one_name_are_refused(package: Path) -> None:
    write(
        package,
        {
            "a/__init__.py": "",
            "a/interface.py": CONTRACT.format(name="CLOCK", contract="clock"),
            "b/__init__.py": "",
            "b/interface.py": CONTRACT.format(name="OTHER", contract="clock"),
        },
    )
    with pytest.raises(InterfaceContractError) as raised:
        interface_contracts(importlib.import_module(package.name))
    assert raised.value.code == "INTERFACE_CONTRACT_INVALID"


def test_every_contract_the_distribution_ships_has_its_kit() -> None:
    assert set(kits(orvex.contracts)) == set(interface_contracts(orvex.contracts))


def test_every_adapter_the_distribution_ships_passes_its_contract_s_whole_kit() -> None:
    shipped = kits(orvex.contracts)
    for name, adapter in adapters(orvex.contracts).items():
        assert adapter.contract is shipped[name].contract
        verify_kit(shipped[name], adapter.make)
