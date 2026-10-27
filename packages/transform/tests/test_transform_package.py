import re

import sextant.transform


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", sextant.transform.__version__)
