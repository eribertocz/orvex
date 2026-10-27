import re

import orvex.semantic


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", orvex.semantic.__version__)
