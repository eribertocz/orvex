import re

import orvex.contracts


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", orvex.contracts.__version__)
