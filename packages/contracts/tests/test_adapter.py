"""Public test adapters (#486 G4): a module runs its own operations with only the contracts it
requires, each supplied by an adapter, and without the modules that only consume it."""

from __future__ import annotations

from decimal import Decimal

import pytest
from orvex.contracts.adapter import ContractAdapter, required_adapters
from orvex.contracts.binding import Bound
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit_run import verify_kit
from orvex.contracts.manifest import Manifest, Required
from orvex.contracts.version import Version

from .sample_contract import ENVELOPE, LEDGER, LEDGER_KIT, READING, MemoryLedger, Reading

LEDGER_ADAPTER = ContractAdapter(LEDGER, MemoryLedger)


def needing(*required: Required) -> Manifest:
    return Manifest("billing", (), required, (), ())


class Billing:
    """A sample module that requires the ledger contract and nothing else."""

    def __init__(self, ledger: Bound) -> None:
        self._ledger = ledger

    def bill(self, name: str, amount: Decimal) -> int:
        number: int = self._ledger.record(ENVELOPE, Reading(name, amount, READING.at))
        return number


def test_a_module_runs_its_own_operations_with_only_its_adapters() -> None:
    manifest = needing(Required("ledger", Version(1, 0), ("write",)))
    given = required_adapters(manifest, {"ledger": LEDGER_ADAPTER})
    assert list(given) == ["ledger"]
    ledger = Bound(LEDGER, given["ledger"].make(), serialized=True)
    assert Billing(ledger).bill("meter-1", Decimal("3.10")) == 1


def test_the_adapter_is_a_full_implementation_that_passes_its_kit() -> None:
    assert verify_kit(LEDGER_KIT, LEDGER_ADAPTER.make)


def test_a_module_without_requirements_needs_no_adapter() -> None:
    assert required_adapters(needing(), {}) == {}


@pytest.mark.parametrize(
    "found", [{}, {"clock": ContractAdapter(LEDGER, MemoryLedger)}], ids=["none", "another"]
)
def test_a_required_contract_without_an_adapter_is_refused(
    found: dict[str, ContractAdapter],
) -> None:
    manifest = needing(Required("ledger", Version(1, 0), ("read",)))
    with pytest.raises(InterfaceContractError) as raised:
        required_adapters(manifest, found)
    assert raised.value.code == "MODULE_ADAPTER_MISSING"
    assert "billing requires ledger 1.0: no public test adapter" in raised.value.message


@pytest.mark.parametrize("version", [Version(1, 2), Version(2, 0)])
def test_an_adapter_at_an_incompatible_version_is_refused(version: Version) -> None:
    with pytest.raises(InterfaceContractError) as raised:
        required_adapters(
            needing(Required("ledger", version, ("read",))), {"ledger": LEDGER_ADAPTER}
        )
    assert raised.value.code == "MODULE_ADAPTER_MISSING"
    assert "the adapter is at 1.1" in raised.value.message
