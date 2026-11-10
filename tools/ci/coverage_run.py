"""Branch coverage of the whole suite in CI (#429): a pytest plugin that measures while
`PYTEST_COVERAGE=1`, which `tools/ci/select.py` sets only when a part runs its share of the
whole suite. Otherwise it does nothing, so local runs and selections stay as fast as before.

    python -m pytest -p tools.ci.coverage_run ...

It starts before collection, so every module the suite imports is measured, with the
`[tool.coverage.run]` settings of `pyproject.toml`, and saves one data file per process.
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
