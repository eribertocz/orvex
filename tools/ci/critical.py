"""The critical tests never pass in CI by not running (#427).

    python -m tools.ci.critical junit.xml [...]

The critical tests are the `critical_tests` of `tools/ci/checks.toml` plus every conformance and
property test. In CI a skip, an expected failure or an unexpected pass of one is a failure: the
pytest plugin `tests.support.critical_tests` turns it into one as it happens, and this command,
run on the junit reports afterwards, fails when any critical test is reported `skipped` (pytest
writes both skips and expected failures that way). The two are independent on purpose. A known
gap in a critical guarantee is only ever an `expected-fail` entry of a normative manifest.
"""

from __future__ import annotations

import sys
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tools.repository import REPOSITORY_ROOT

ALWAYS = ("tests/conformance", "tests/properties", "conformance")


def critical_paths(root: Path = REPOSITORY_ROOT) -> tuple[str, ...]:
    """The repository-relative paths (files or folders) whose tests are critical."""
    document = tomllib.loads((root / "tools" / "ci" / "checks.toml").read_text("utf-8"))
    listed = document.get("profiles", {}).get("critical_tests", [])
    return tuple(dict.fromkeys([*(str(p).rstrip("/") for p in listed), *ALWAYS]))


def is_critical(relative: str, paths: tuple[str, ...]) -> bool:
    """Whether the test file at `relative` (POSIX, from the repository root) is critical."""
    return any(relative == p or relative.startswith(p + "/") for p in paths)


def skipped(report: Path, paths: tuple[str, ...]) -> list[str]:
    """The critical test cases a junit report shows `skipped`."""
    found = []
    for case in ElementTree.parse(report).iter("testcase"):
        if case.find("skipped") is None:
            continue
        # A module skipped at collection has no class name: pytest puts the module in `name`.
        module = str(case.get("classname") or case.get("name") or "").split("[")[0]
        candidates = {module.replace(".", "/") + ".py", str(case.get("file", ""))}
        parts = module.split(".")
        candidates |= {"/".join(parts[:n]) + ".py" for n in range(1, len(parts) + 1)}
        if any(is_critical(c, paths) for c in candidates if c):
            found.append(f"{case.get('classname')}::{case.get('name')}")
    return found


def main(arguments: list[str]) -> int:
    paths = critical_paths()
    found = [s for report in arguments for s in skipped(Path(report), paths)]
    for case in found:
        print(f"CRITICAL_TEST_NOT_RUN {case}")
    print(f"critical tests: {len(found)} not run in {len(arguments)} report(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
