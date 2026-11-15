"""`orvex.transform.api.Transform` passes the whole transform kit (#486 G1, G3, G5): every case,
in process and serialized, with nothing skipped; and a planted wrong implementation fails it."""

from __future__ import annotations

import pytest
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit_run import verify_kit
from orvex.contracts.transform.interface import TRANSFORM
from orvex.contracts.transform.kit import TRANSFORM_KIT
from orvex.transform.api import Transform


def test_the_whole_kit_passes_in_process_and_serialized() -> None:
    reports = verify_kit(TRANSFORM_KIT, Transform)
    assert sorted({report.serialized for report in reports}) == [False, True]
    for report in reports:
        assert [result.id for result in report.results] == [c.id for c in TRANSFORM_KIT.cases]
        assert all(result.passed for result in report.results)
        assert report.envelope_results
        assert all(r.passed for r in report.envelope_results)
    assert TRANSFORM_KIT.recorded == len(TRANSFORM_KIT.cases) == 37


def test_every_operation_is_implemented() -> None:
    missing = [o.name for o in TRANSFORM.operations if not callable(getattr(Transform, o.name))]
    assert missing == []


class _Floated(Transform):
    """A planted wrong implementation: `$since` loses its time zone."""

    def since_value(self, envelope, kind, watermark, lookback):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        return super().since_value(envelope, kind, watermark, lookback).replace("+00:00", "")


class _Live(Transform):
    """A planted implementation that returns a live object, which cannot cross (G5)."""

    def lineage_project_key(self, envelope, name):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        return iter(name)


@pytest.mark.parametrize(
    ("planted", "case"),
    [(_Floated, "since-value.lookback"), (_Live, "lineage-project-key.slug")],
)
def test_a_planted_wrong_implementation_fails_the_kit(planted: type, case: str) -> None:
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(TRANSFORM_KIT, planted)
    assert raised.value.code == "KIT_FAILED"
    assert f"KIT_CASE_FAILED: {case}" in raised.value.message
