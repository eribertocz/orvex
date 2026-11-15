"""#486 G1, G3, G5: the store's implementation passes its contract's whole kit, in process and
serialized, and the kit catches an implementation that breaks one of its guarantees."""

from __future__ import annotations

from importlib.resources import files

import pyarrow as pa
import pytest
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.kit_run import kit_problems, run_kit, verify_kit
from orvex.contracts.manifest import parse_manifest
from orvex.contracts.store.interface import STORE
from orvex.contracts.store.kit import STORE_KIT
from orvex.contracts.store.v1 import StatementRequest
from orvex.contracts.store.values import ReadHandle, TransactionHandle
from orvex.store import api
from orvex.store.api import Store


def test_the_store_passes_its_whole_kit_in_process_and_serialized() -> None:
    reports = verify_kit(STORE_KIT, Store)
    assert {report.serialized for report in reports} == {False, True}
    for report in reports:
        assert len(report.results) == STORE_KIT.recorded == len(STORE_KIT.cases)
        assert all(result.passed for result in (*report.results, *report.envelope_results))


def test_the_manifest_provides_every_capability_from_the_api_package() -> None:
    manifest = parse_manifest(files("orvex.store").joinpath("module.toml").read_text(), "store")
    (provided,) = manifest.provides
    assert provided.contract == STORE.name
    assert provided.version == STORE.version
    assert set(provided.capabilities) == set(STORE.capabilities)
    assert getattr(api, provided.implementation) is Store
    assert manifest.requires == ()


class _ExecutesTwice(Store):
    """Planted: an execute that ignores its idempotency key."""

    def execute(
        self, envelope: CallEnvelope, transaction: TransactionHandle, request: StatementRequest
    ) -> None:
        super().execute(envelope, transaction, request)
        super().execute(
            CallEnvelope(**{**_fields(envelope), "idempotency_key": None}), transaction, request
        )


class _DecimalAsFloat(Store):
    """Planted: a read that turns every DECIMAL into a float."""

    def read_batch(self, envelope: CallEnvelope, read: ReadHandle) -> pa.Table | None:
        found = super().read_batch(envelope, read)
        if found is None:
            return None
        columns = [
            column.cast(pa.float64()) if pa.types.is_decimal(column.type) else column
            for column in found.columns
        ]
        return pa.table(columns, names=found.column_names)


class _ForgedHandleAccepted(Store):
    """Planted: a forged read handle answered as an ended read instead of refused."""

    def read_batch(self, envelope: CallEnvelope, read: ReadHandle) -> pa.Table | None:
        if read.id == "read-forged":
            return None
        return super().read_batch(envelope, read)


def _fields(envelope: CallEnvelope) -> dict[str, object]:
    return {name: getattr(envelope, name) for name in CallEnvelope.__slots__}


@pytest.mark.parametrize(
    ("planted", "case"),
    [
        (_ExecutesTwice, "execute.idempotent"),
        (_DecimalAsFloat, "start-read.exact"),
        (_ForgedHandleAccepted, "read-batch.ends"),
    ],
)
def test_the_kit_fails_a_planted_broken_guarantee(planted: type[Store], case: str) -> None:
    report = run_kit(STORE_KIT, planted, serialized=False)
    failed = [problem for problem in kit_problems(STORE_KIT, report) if f": {case} (" in problem]
    assert failed, f"the kit did not catch the plant in {case}"
