"""The implementations chosen by configuration (#486 G1, §2.2): `<SLUG>_IMPLEMENTATION_<CONTRACT>`
names the module whose implementation of a contract the server is assembled with, by
configuration only; a choice that is not a module name, or that no installed module honours,
stops the server; an alternative that omits an operation is refused."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.manifest import Manifest, Provided, Required
from orvex.contracts.semantic.interface import RESULT_CACHE, Stored, Use
from orvex.contracts.version import Version
from orvex.server.assembly import AssemblyRefused, assembly_problems
from orvex.server.identity import load_identity
from orvex.server.implementations import configured_choices, implementation_of


def codes(found: list[str]) -> list[str]:
    return [problem.split(":", 1)[0] for problem in found]


def variable() -> str:
    return f"{load_identity().slug.upper().replace('-', '_')}_IMPLEMENTATION_RESULT_CACHE"


def test_the_choice_is_read_from_the_environment_by_contract() -> None:
    environment = {variable(): "alternative_result_cache_ipc", "OTHER_IMPLEMENTATION_X": "y"}
    assert configured_choices(load_identity(), environment) == {
        "result-cache": "alternative_result_cache_ipc"
    }


@pytest.mark.parametrize("value", ["", "Alpha", "a-b", "a b"])
def test_a_choice_that_is_not_a_module_name_is_refused(value: str) -> None:
    with pytest.raises(AssemblyRefused) as raised:
        configured_choices(load_identity(), {variable(): value})
    assert codes(raised.value.problems) == ["ASSEMBLY_CHOICE_INVALID"]


def test_without_a_choice_the_installed_result_cache_is_the_semantic_module_s() -> None:
    implementation = implementation_of(load_identity(), "result-cache", {})
    assert implementation.__module__.split(".")[1] == "semantic"


def test_a_choice_that_is_not_installed_stops_the_server() -> None:
    with pytest.raises(AssemblyRefused) as raised:
        implementation_of(load_identity(), "result-cache", {variable(): "absent"})
    assert codes(raised.value.problems) == ["ASSEMBLY_CHOICE_UNKNOWN"]


def test_a_contract_nobody_provides_stops_the_server() -> None:
    with pytest.raises(AssemblyRefused) as raised:
        implementation_of(load_identity(), "no-such-contract", {})
    assert codes(raised.value.problems) == ["ASSEMBLY_NO_IMPLEMENTATION"]


class WithoutEntries:
    """A planted alternative, exact in every operation but one: it omits `entries`."""

    def __init__(self, max_bytes: int, clock: Callable[[], float]) -> None:
        self.max_bytes = max_bytes

    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None:
        return None

    def put(self, envelope: CallEnvelope, use: Use, stored: Stored) -> None:
        return None

    def size_bytes(self, envelope: CallEnvelope) -> int:
        return 0


def test_an_alternative_that_omits_an_operation_is_refused_at_assembly() -> None:
    capabilities = RESULT_CACHE.capabilities
    provider = Manifest(
        "alternative_result_cache_ipc",
        (Provided(RESULT_CACHE.name, Version(1, 0), capabilities, "IpcResultCache"),),
        (),
        (),
        (),
    )
    consumer = Manifest(
        "semantic", (), (Required(RESULT_CACHE.name, Version(1, 0), capabilities),), (), ()
    )
    found = assembly_problems(
        (provider, consumer),
        {RESULT_CACHE.name: RESULT_CACHE},
        {("alternative_result_cache_ipc", RESULT_CACHE.name): WithoutEntries},
        {RESULT_CACHE.name: "alternative_result_cache_ipc"},
    )
    assert [problem.split(":", 1)[0] for problem in found] == ["ASSEMBLY_OPERATION_MISSING"]
