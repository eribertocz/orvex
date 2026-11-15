"""A kit case with its own environment runs in a process built from that declaration alone, and
its result returns through the serializing codec (#486 G1)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from orvex.contracts import kit_run
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit import CaseEnvironment, Kit
from orvex.contracts.kit_case import run_case
from orvex.contracts.kit_process import run_case_in_its_own_process
from orvex.contracts.kit_run import run_kit, verify_kit
from orvex.contracts.version import Version

from .environment_contract import SOURCE, STATION, STATION_KIT, EnvironmentStation

V10 = Version(1, 0)


@pytest.fixture(autouse=True)
def extension_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kit's own extension directory: httpfs and json, one level down as DuckDB keeps them."""
    versioned = tmp_path / "v2.0.0" / "linux_amd64"
    versioned.mkdir(parents=True)
    for name in ("httpfs", "json"):
        (versioned / f"{name}.duckdb_extension").write_bytes(b"\0")
    monkeypatch.setenv(SOURCE, str(tmp_path))


def test_each_case_sees_exactly_its_declared_environment_in_both_bindings() -> None:
    reports = verify_kit(STATION_KIT, EnvironmentStation)
    assert {report.serialized for report in reports} == {False, True}
    assert all(r.passed for report in reports for r in report.results)


def test_a_case_whose_declared_environment_is_ignored_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kit_run, "run_case_in_its_own_process", run_case)
    report = run_kit(STATION_KIT, EnvironmentStation, serialized=True)
    assert [r.id for r in report.results if not r.passed] == [
        "setting.declared-engine",
        "extensions.without-httpfs",
    ]
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(STATION_KIT, EnvironmentStation)
    assert raised.value.code == "KIT_FAILED"


def test_a_factory_the_process_cannot_import_fails_the_case() -> None:
    report = run_kit(STATION_KIT, lambda: EnvironmentStation(), serialized=False)
    assert all(not r.passed for r in report.results)
    assert "KIT_CASE_NOT_IMPORTABLE" in report.results[0].detail


class Broken:
    def __init__(self) -> None:
        raise RuntimeError("cannot start")


def test_a_process_that_fails_fails_the_case() -> None:
    case = STATION_KIT.cases[0]
    result = run_case_in_its_own_process(STATION_KIT, case, Broken, True, V10, V10)
    assert result.outcome == "failed"
    assert "KIT_CASE_PROCESS_FAILED" in result.detail
    assert "cannot start" in result.detail


def test_a_missing_extension_source_fails_the_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOURCE)
    report = run_kit(STATION_KIT, EnvironmentStation, serialized=False)
    failed = {r.id: r.detail for r in report.results if not r.passed}
    assert list(failed) == ["extensions.without-httpfs"]
    assert "KIT_CASE_PROCESS_FAILED" in failed["extensions.without-httpfs"]


def test_the_environment_is_part_of_the_kit_s_description() -> None:
    cases = STATION_KIT.describe()["cases"]
    assert isinstance(cases, dict)
    assert cases["setting.declared-engine"]["environment"] == {
        "variables": {"STATION_ENGINE_VERSION": "26.10"},
        "extensions": None,
    }
    assert cases["extensions.without-httpfs"]["environment"]["extensions"] == {
        "variable": "STATION_EXTENSIONS",
        "source": SOURCE,
        "without": ["httpfs"],
    }


@pytest.mark.parametrize(
    "variables",
    [(("lower", "1"),), (("A", "1"), ("A", "2")), (("STATION_EXTENSIONS", "x"),)],
    ids=["lowercase", "twice", "clashes-with-the-directory"],
)
def test_an_ill_formed_environment_is_refused(variables: tuple[tuple[str, str], ...]) -> None:
    case = STATION_KIT.cases[2]
    environment = CaseEnvironment(
        variables, case.environment.extensions if case.environment else None
    )
    changed = dataclasses.replace(case, environment=environment)
    with pytest.raises(InterfaceContractError) as raised:
        Kit(STATION, recorded=3, cases=(*STATION_KIT.cases[:2], changed))
    assert raised.value.code == "KIT_INVALID"
