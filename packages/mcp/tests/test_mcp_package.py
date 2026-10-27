import re

import sextant.mcp


def test_version_comes_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", sextant.mcp.__version__)
