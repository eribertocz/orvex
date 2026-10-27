import re

import sextant.access


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", sextant.access.__version__)
