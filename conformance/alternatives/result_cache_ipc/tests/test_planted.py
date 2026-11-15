"""Each planted fault of an alternative result cache fails the kit (#486 G1, §4): a hit after
its `ttl`, a cache over its byte bound, a `DECIMAL` decoded as a float, and a match by a
prefix of the key. Each plant is the alternative with one guarantee broken."""

from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from orvex.alternative_result_cache_ipc import ipc_cache
from orvex.alternative_result_cache_ipc.api import IpcResultCache
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit_run import verify_kit
from orvex.contracts.semantic.interface import Stored, Use
from orvex.contracts.semantic.kit import RESULT_CACHE_KIT, maker


class HitAfterTtl(IpcResultCache):
    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None:
        return super().get(envelope, Use(use.key, None))


class OverBound(IpcResultCache):
    def __init__(self, max_bytes: int, clock: Callable[[], float]) -> None:
        super().__init__(2 * max_bytes, clock)


class PrefixMatch(IpcResultCache):
    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None:
        key = use.key
        if key is not None and key not in self._held:
            key = next((k for k in sorted(self._held) if k.startswith(key)), key)
        return super().get(envelope, Use(key, use.ttl))


def _float_decimals(ipc: bytes) -> pa.Table:
    table = pa.ipc.open_stream(pa.py_buffer(ipc)).read_all()
    for index, field in enumerate(table.schema):
        if pa.types.is_decimal(field.type):
            column = pc.cast(table.column(index), pa.float64())
            table = table.set_column(index, pa.field(field.name, pa.float64()), column)
    return table


PLANTS = {
    "hit-after-ttl": (HitAfterTtl, "get.expires-by-ttl"),
    "over-bound": (OverBound, "put.bounded-never-raises"),
    "prefix-match": (PrefixMatch, "get.equal-key-only"),
}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_a_planted_fault_fails_its_kit_case(plant: str) -> None:
    implementation, case = PLANTS[plant]
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(RESULT_CACHE_KIT, maker(implementation))
    assert raised.value.code == "KIT_FAILED"
    assert f"KIT_CASE_FAILED: {case} " in raised.value.message


def test_a_decimal_decoded_as_a_float_fails_the_exactness_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ipc_cache, "_decode", _float_decimals)
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(RESULT_CACHE_KIT, maker(IpcResultCache))
    assert "KIT_CASE_FAILED: get.exact-hit " in raised.value.message


def test_a_run_with_one_case_fewer_fails() -> None:
    from orvex.contracts.kit_run import kit_problems, run_kit

    report = run_kit(RESULT_CACHE_KIT, maker(IpcResultCache), serialized=False)
    short = type(report)(
        report.contract,
        report.serialized,
        report.consumer,
        report.provided,
        report.results[:-1],
        report.envelope_results,
    )
    assert kit_problems(RESULT_CACHE_KIT, report) == []
    assert any(p.startswith("KIT_RUN_INCOMPLETE") for p in kit_problems(RESULT_CACHE_KIT, short))
