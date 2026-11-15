"""The independence check of an alternative (#486 G1, §4): the real alternative passes, and each
planted violation fails it: an import of an internal of the module it replaces, an import of
another module, a dynamic import, and a block of code copied from the replaced module."""

from __future__ import annotations

import pytest

from tools.checks.alternative_independence import (
    ALTERNATIVES,
    WINDOW,
    code_lines,
    import_findings,
    independence_scan,
    shared_findings,
)
from tools.repository import REPOSITORY_ROOT

ALTERNATIVE = ALTERNATIVES[0]
ORIGINAL = REPOSITORY_ROOT / "packages/semantic/src/orvex/semantic/result_cache.py"


def test_the_real_alternative_is_independent() -> None:
    assert independence_scan() == []


@pytest.mark.parametrize(
    "source",
    [
        "from orvex.semantic.result_cache import ResultCache\n",
        "import orvex.semantic\n",
        "from orvex.semantic import api\n",
        "from orvex.store.lake import Lake\n",
        "import duckdb\n",
        "import importlib\nimportlib.import_module('orvex.semantic.result_cache')\n",
    ],
)
def test_a_planted_import_fails(source: str) -> None:
    found = import_findings(source, "x.py", ALTERNATIVE, "orvex")
    assert [finding.code for finding in found] == ["ALTERNATIVE_IMPORT_FORBIDDEN"]


@pytest.mark.parametrize(
    "source",
    [
        "from orvex.contracts.semantic.interface import Stored\n",
        "import heapq\nimport pyarrow as pa\n",
        "from orvex.alternative_result_cache_ipc.ipc_cache import IpcResultCache\n",
    ],
)
def test_the_allowed_imports_pass(source: str) -> None:
    assert import_findings(source, "x.py", ALTERNATIVE, "orvex") == []


def test_a_block_copied_from_the_original_fails() -> None:
    original = ORIGINAL.read_text(encoding="utf-8")
    start = original.index("    def put(")
    copied = "class Copy:\n" + original[start : original.index("    def _drop(", start)]
    found = shared_findings({"alt.py": copied}, {"result_cache.py": original})
    assert found
    assert {finding.code for finding in found} == {"ALTERNATIVE_CODE_SHARED"}


def test_a_copy_with_other_comments_and_spacing_is_still_found() -> None:
    block = "x = 1\nif x:\n    y = x + 1\nz = y * 2\nprint(z)\n"
    disguised = '"""Doc."""\nx  =  1  # one\nif x:\n\n    y = x+1\nz = y * 2\nprint( z )\n'
    assert len(code_lines(block)) > WINDOW
    found = shared_findings({"alt.py": disguised}, {"orig.py": block})
    assert [finding.line for finding in found] == [2, 3]
