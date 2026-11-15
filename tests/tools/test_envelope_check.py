"""ENVELOPE_BUILT_BY_HAND (#486 §2 item 6): only contracts/context.py builds an envelope; each
planted hand-built envelope in product code fails the boundaries check."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.checks.boundaries import run
from tools.checks.envelopes import envelope_scan

from .test_checks import make_repository

CONTEXT = "packages/contracts/src/orvex/contracts/context.py"
NAMES = "from orvex.contracts.envelope import CallEnvelope\n"

PLANTED = {
    "server builds one": (
        "packages/server/src/orvex/server/x.py",
        NAMES + 'e = CallEnvelope("a", "1.0", "c", "u", "p", "r")\n',
    ),
    "through the module": (
        "tools/x.py",
        "from orvex.contracts import envelope\ne = envelope.CallEnvelope()\n",
    ),
    "a runner copies one": (
        "conformance/runners/x.py",
        NAMES + "import dataclasses\nf = dataclasses.replace(e, actor='x')\n",
    ),
    "imported replace": (
        "packages/store/src/orvex/store/x.py",
        NAMES + "from dataclasses import replace as copy\nf = copy(e, actor='x')\n",
    ),
    "the kit client": (
        "packages/contracts/src/orvex/contracts/kit_client.py",
        NAMES + "import dataclasses\nf = dataclasses.replace(e, actor=None)\n",
    ),
}

# Every repository holds at least one module, as the module rule expects.
PACKAGE = {"packages/store/src/orvex/store/__init__.py": ""}

ALLOWED = {
    "the builder": (CONTEXT, NAMES + 'e = CallEnvelope("a", "1.0", "c", "u", "p", "r")\n'),
    "a test plants one": ("tests/x/test_y.py", NAMES + "e = CallEnvelope()\n"),
    "a module test": ("packages/store/tests/test_x.py", NAMES + "e = CallEnvelope()\n"),
    "another dataclass": ("tools/x.py", "import dataclasses\nf = dataclasses.replace(a, b=1)\n"),
    "a string": ("tools/x.py", NAMES + "s = 'a'.replace('a', 'b')\n"),
}


@pytest.mark.parametrize(("relative", "source"), PLANTED.values(), ids=PLANTED.keys())
def test_a_hand_built_envelope_fails_the_check(tmp_path: Path, relative: str, source: str) -> None:
    root = make_repository(tmp_path, {**PACKAGE, relative: source})
    assert [(f.code, f.path) for f in envelope_scan(root)] == [("ENVELOPE_BUILT_BY_HAND", relative)]
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    assert run(root, empty, empty) == 1


@pytest.mark.parametrize(("relative", "source"), ALLOWED.values(), ids=ALLOWED.keys())
def test_the_builder_tests_and_other_copies_are_allowed(
    tmp_path: Path, relative: str, source: str
) -> None:
    assert envelope_scan(make_repository(tmp_path, {relative: source})) == []
