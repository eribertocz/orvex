import re

import sextant.semantic


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", sextant.semantic.__version__)
