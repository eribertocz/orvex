"""The semantic result cache passes the whole result-cache kit (#486 G1), in process and
serialized, with every recorded case run and none skipped, and its `api` package and manifest
offer exactly it."""

from __future__ import annotations

from importlib.resources import files

import orvex.semantic
from orvex.contracts.kit_run import verify_kit
from orvex.contracts.manifest import parse_manifest
from orvex.contracts.semantic.interface import RESULT_CACHE
from orvex.contracts.semantic.kit import RESULT_CACHE_KIT, maker
from orvex.semantic import api


def test_the_cache_passes_every_case_of_the_kit_in_both_bindings() -> None:
    reports = verify_kit(RESULT_CACHE_KIT, maker(api.ResultCache))
    assert {report.serialized for report in reports} == {False, True}
    for report in reports:
        assert len(report.results) == RESULT_CACHE_KIT.recorded == 12
        assert all(result.passed for result in report.results)
        assert report.envelope_results
        assert all(result.passed for result in report.envelope_results)


def test_the_manifest_provides_the_cache_from_the_api_package() -> None:
    text = files(orvex.semantic).joinpath("module.toml").read_text("utf-8")
    manifest = parse_manifest(text, "semantic")
    (provided,) = manifest.provides
    assert (provided.contract, str(provided.version)) == (RESULT_CACHE.name, "1.0")
    assert set(provided.capabilities) == set(RESULT_CACHE.capabilities)
    assert getattr(api, provided.implementation) is api.ResultCache
    assert api.__all__ == ["ResultCache"]
