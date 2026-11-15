"""The alternative result cache passes the whole result-cache kit (#486 G1), in process and
serialized, with every recorded case run, and its `api` package and manifest offer exactly
it."""

from __future__ import annotations

from importlib.resources import files

import orvex.alternative_result_cache_ipc
from orvex.alternative_result_cache_ipc import api
from orvex.contracts.kit_run import verify_kit
from orvex.contracts.manifest import parse_manifest
from orvex.contracts.semantic.interface import RESULT_CACHE
from orvex.contracts.semantic.kit import RESULT_CACHE_KIT, maker


def test_the_alternative_passes_every_case_of_the_kit_in_both_bindings() -> None:
    reports = verify_kit(RESULT_CACHE_KIT, maker(api.IpcResultCache))
    assert {report.serialized for report in reports} == {False, True}
    for report in reports:
        assert len(report.results) == RESULT_CACHE_KIT.recorded
        assert all(result.passed for result in report.results)
        assert all(result.passed for result in report.envelope_results)


def test_the_manifest_provides_every_capability_from_the_api_package() -> None:
    text = files(orvex.alternative_result_cache_ipc).joinpath("module.toml").read_text("utf-8")
    manifest = parse_manifest(text, "alternative_result_cache_ipc")
    (provided,) = manifest.provides
    assert provided.contract == RESULT_CACHE.name
    assert set(provided.capabilities) == set(RESULT_CACHE.capabilities)
    assert manifest.requires == ()
    assert getattr(api, provided.implementation) is api.IpcResultCache
