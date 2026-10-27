import re

import orvex.server


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", orvex.server.__version__)
