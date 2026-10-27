import re

import sextant.dashboards


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", sextant.dashboards.__version__)
