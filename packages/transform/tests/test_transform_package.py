import re

import orvex.transform


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", orvex.transform.__version__)
