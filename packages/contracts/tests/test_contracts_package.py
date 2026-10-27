import re

import sextant.contracts


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", sextant.contracts.__version__)
