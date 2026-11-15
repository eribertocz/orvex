"""#486 step 4: `orvex.access.api.Access` passes the whole `access` and `audit-log` kits, in
process and serialized; its manifest provides both contracts from the `api` package; and an
implementation that breaks one guarantee fails its kit."""

from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files

import pytest
from orvex.access import api
from orvex.access.api import Access
from orvex.contracts.access.errors import ACCESS_FORBIDDEN, AccessError
from orvex.contracts.access.interface import ACCESS
from orvex.contracts.access.kit import ACCESS_KIT
from orvex.contracts.access.kit_world import KIT_CLOCK, KitProviderFetcher
from orvex.contracts.audit_log.interface import AUDIT_LOG
from orvex.contracts.audit_log.kit import AUDIT_LOG_KIT
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit_run import kit_problems, run_kit, verify_kit
from orvex.contracts.manifest import parse_manifest
from orvex.contracts.version import Version


def made() -> Access:
    return Access(fetcher=KitProviderFetcher(), clock=KIT_CLOCK)


def test_the_access_kit_passes_in_process_and_serialized() -> None:
    reports = verify_kit(ACCESS_KIT, made)
    assert {report.serialized for report in reports} == {False, True}
    assert all(len(report.results) == ACCESS_KIT.recorded for report in reports)


def test_the_audit_log_kit_passes_in_process_and_serialized() -> None:
    reports = verify_kit(AUDIT_LOG_KIT, made)
    assert {report.serialized for report in reports} == {False, True}
    assert all(len(report.results) == AUDIT_LOG_KIT.recorded for report in reports)


def test_the_recorded_case_counts() -> None:
    assert (ACCESS_KIT.recorded, AUDIT_LOG_KIT.recorded) == (69, 20)


def test_the_manifest_provides_both_contracts_from_the_api_package() -> None:
    text = files("orvex.access").joinpath("module.toml").read_text("utf-8")
    manifest = parse_manifest(text, "access")
    provided = {entry.contract: entry for entry in manifest.provides}
    assert set(provided) == {"access", "audit-log"}
    for contract in (ACCESS, AUDIT_LOG):
        entry = provided[contract.name]
        assert entry.version == contract.version
        assert set(entry.capabilities) == set(contract.capabilities)
        implementation = getattr(api, entry.implementation)
        assert all(callable(getattr(implementation, o.name)) for o in contract.operations)
    assert manifest.requires == ()


class _Twice(Access):
    """Plant: every repeat with one idempotency key runs again."""

    def _once[T](
        self,
        operation: str,
        envelope: CallEnvelope,
        arguments: tuple[object, ...],
        work: Callable[[], T],
    ) -> T:
        return work()


class _NoRevocation(Access):
    """Plant: deactivating a user leaves their sessions live."""

    def deactivate_user(self, envelope: CallEnvelope, instance: object, user: str) -> None:
        return None


class _OpenContext(Access):
    """Plant: a context is built for a caller with no role."""

    def server_context(self, envelope: CallEnvelope, instance: object, *rest: object) -> str:
        try:
            return super().server_context(envelope, instance, *rest)  # type: ignore[arg-type]
        except AccessError as error:
            if error.code != ACCESS_FORBIDDEN:
                raise
            return "{}"


@pytest.mark.parametrize(
    ("plant", "failing"),
    [
        (_Twice, "login.idempotent"),
        (_NoRevocation, "deactivate-user.deactivation-revokes"),
        (_OpenContext, "audience-of.no-role-no-context"),
    ],
)
def test_an_implementation_that_breaks_a_guarantee_fails_its_kit(
    plant: type[Access], failing: str
) -> None:
    report = run_kit(
        ACCESS_KIT, lambda: plant(fetcher=KitProviderFetcher(), clock=KIT_CLOCK), serialized=False
    )
    problems = kit_problems(ACCESS_KIT, report)
    assert any(failing in problem for problem in problems), problems


def test_a_kit_with_one_case_fewer_is_refused() -> None:
    from orvex.contracts.kit import Kit

    with pytest.raises(InterfaceContractError) as refused:
        Kit(ACCESS, recorded=ACCESS_KIT.recorded, cases=ACCESS_KIT.cases[1:])
    assert refused.value.code in {"KIT_CASE_COUNT_CHANGED", "KIT_OPERATION_UNCOVERED"}


def test_a_call_without_its_idempotency_key_never_reaches_the_implementation() -> None:
    from orvex.contracts.binding import Bound

    bound = Bound(ACCESS, made(), serialized=True)
    envelope = CallEnvelope("access", "1.0", "c", "system", "p", "r", None)
    with pytest.raises(InterfaceContractError) as refused:
        bound.create_user(envelope, None, "x", "X")
    assert refused.value.code == "CONTRACT_ENVELOPE_INVALID"
    assert Version(1, 0) == ACCESS.version
