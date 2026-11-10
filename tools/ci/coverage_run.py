"""Branch coverage of the whole suite in CI (#429): a pytest plugin that measures while
`PYTEST_COVERAGE=1`, which `tools/ci/select.py` sets only when a part runs its share of the
whole suite. Otherwise it does nothing, so local runs and selections stay as fast as before.

    python -m pytest -p tools.ci.coverage_run ...

It starts before collection, so every module the suite imports is measured, with the
`[tool.coverage.run]` settings of `pyproject.toml`, and saves one data file per process.

While measuring, Hypothesis is derandomized (its seed comes from each test's name) and keeps no
example database, so the same commit measures the same coverage on every run: the floors are
compared with a reproducible value (#429). Selections and local runs keep random examples, and
the nightly property run explores.
"""

from __future__ import annotations

import os

import pytest

from tools.repository import REPOSITORY_ROOT

_MEASURING = pytest.StashKey[object]()


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("PYTEST_COVERAGE") != "1":
        return
    import coverage  # only where coverage is measured: the dev group installs it
    import hypothesis

    hypothesis.settings.register_profile(
        "coverage", parent=hypothesis.settings(), derandomize=True, database=None
    )
    hypothesis.settings.load_profile("coverage")
    work = coverage.Coverage(config_file=str(REPOSITORY_ROOT / "pyproject.toml"))
    work.start()
    config.stash[_MEASURING] = work


def pytest_unconfigure(config: pytest.Config) -> None:
    work = config.stash.get(_MEASURING, None)
    if work is not None:
        import coverage

        assert isinstance(work, coverage.Coverage)
        work.stop()
        work.save()
