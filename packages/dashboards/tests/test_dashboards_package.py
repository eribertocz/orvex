import re

import orvex.dashboards


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", orvex.dashboards.__version__)
