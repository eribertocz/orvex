"""Tier C Dependabot updates: whether a pull request may merge by itself (git-workflow.md §3.5).

Only patch and minor updates of Python (`pip`) and npm packages qualify, and only when the pull
request touches no file under `.github/` (those are governed: the Owner merges them). Everything
else is left for review, with the reason written to the job log. Enabling auto-merge only asks
GitHub to merge once every required check and rule passes; it never bypasses them.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from github_api import GitHubApiError, GitHubClient
from governance import GovernanceError, changed_paths

DEPENDABOT = "dependabot[bot]"
ECOSYSTEMS = frozenset({"pip", "npm_and_yarn"})
UPDATE_TYPES = frozenset({"version-update:semver-patch", "version-update:semver-minor"})
GOVERNED_PREFIX = ".github/"


@dataclass(frozen=True, slots=True)
class Decision:
    merge: bool
    reason: str


def decide(author: str, ecosystem: str, update_type: str, files: Sequence[str]) -> Decision:
    """Auto-merge, or leave the pull request for review, and why."""
    if author != DEPENDABOT:
        return Decision(False, "not Dependabot")
    if ecosystem not in ECOSYSTEMS:
        return Decision(False, f"not pip or npm ({ecosystem or 'unknown ecosystem'})")
    if not update_type:
        return Decision(False, "unknown update type")
    if update_type not in UPDATE_TYPES:
        kind = update_type.removeprefix("version-update:semver-")
        return Decision(False, kind if kind == "major" else f"not patch or minor ({update_type})")
    governed = [path for path in files if path.startswith(GOVERNED_PREFIX)]
    if governed:
        return Decision(False, f"governed path ({governed[0]})")
    if not files:
        return Decision(False, "no changed files listed")
    return Decision(True, f"{ecosystem} {update_type.removeprefix('version-update:semver-')}")


def main() -> int:
    try:
        author = os.environ["PR_AUTHOR"]
        ecosystem = os.environ.get("ECOSYSTEM", "")
        update_type = os.environ.get("UPDATE_TYPE", "")
        client = GitHubClient(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
        pull_number = int(os.environ["PULL_NUMBER"])
        output = os.environ["GITHUB_OUTPUT"]
    except (KeyError, ValueError) as error:
        print(f"::error::AUTO_MERGE_MISCONFIGURED: missing or invalid environment value {error}")
        return 1
    try:
        files = changed_paths(client, pull_number)
    except (GovernanceError, GitHubApiError) as error:
        print(f"::error::{error}")
        return 1
    decision = decide(author, ecosystem, update_type, files)
    print(f"{'auto-merge' if decision.merge else 'leave'}: {decision.reason}")
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"merge={'true' if decision.merge else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
