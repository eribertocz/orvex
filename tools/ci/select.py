"""Which tests one part of the `python` workflow runs (#391), written to `$GITHUB_ENV`.

    python -m tools.ci.select    # in CI, with PART and GITHUB_EVENT_NAME set

- On `main` (every push), every Python runs the whole suite in `PARTS` parts.
- On a pull request, every Python runs what `pr` runs locally (`tools/ci/verify.py`): the
  critical tests and those the change can affect, spread over the `PARTS` parts by their
  recorded durations. When the impact is uncertain, it runs the whole suite (Owner's decision,
  2026-11-08: fast pull requests; `main` runs the whole suite on every push, so anything a
  selection misses is caught there at once).

Coverage of the critical sources is measured only when a part runs its share of the whole suite
(`PYTEST_COVERAGE=1`, #429): a selection would measure less and fail the floors.

The printed reasons say what was chosen and why. A selected path that is not a plain repository
path selects the whole suite: nothing from a pull request reaches a shell unchecked.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tools.ci import impact, verify
from tools.ci.parts import PARTS
from tools.repository import REPOSITORY_ROOT

PLAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


@dataclass(frozen=True, slots=True)
class Choice:
    shard: str  # `PYTEST_SHARD`: "<part>/<parts>"
    paths: tuple[str, ...]  # nothing: the configured suite
    reasons: tuple[str, ...]


def choose(event: str, part: int, changed: Sequence[str] | None) -> Choice:
    """`changed` is read only for a pull request."""
    whole = Choice(f"{part}/{PARTS}", (), ())
    if event != "pull_request" or changed is None:
        return whole
    paths, reasons = verify.pr_tests(REPOSITORY_ROOT, changed)
    if paths == ["."] or not all(PLAIN.fullmatch(p) and ".." not in p for p in paths):
        return Choice(whole.shard, (), (*reasons, "the whole suite"))
    return Choice(whole.shard, tuple(paths), tuple(reasons))


def lines(choice: Choice) -> list[str]:
    return [
        f"PYTEST_SHARD={choice.shard}",
        f"PYTEST_PATHS={' '.join(choice.paths)}",
        f"PYTEST_COVERAGE={0 if choice.paths else 1}",
    ]


def main(environment: Mapping[str, str] | None = None) -> int:
    found = os.environ if environment is None else environment
    event = found.get("GITHUB_EVENT_NAME", "")
    changed = impact.changed_files(REPOSITORY_ROOT) if event == "pull_request" else None
    choice = choose(event, int(found.get("PART", "0")), changed)
    for reason in choice.reasons:
        print(f"  {reason}")
    output = found.get("GITHUB_ENV")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write("\n".join(lines(choice)) + "\n")
    print("\n".join(lines(choice)))
    return 0


def _whole(environment: Mapping[str, str]) -> int:
    """No history to compare with: this part runs its share of the whole suite."""
    choice = choose("push", int(environment.get("PART", "0")), None)
    output = environment.get("GITHUB_ENV")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write("\n".join(lines(choice)) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        print(f"select: {error}; the whole suite")
        sys.exit(_whole(os.environ))
