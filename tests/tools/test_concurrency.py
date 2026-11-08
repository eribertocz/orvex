"""#386: a newer commit on a pull request cancels that pull request's older runs, and nothing
else is ever cancelled."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.repository import REPOSITORY_ROOT

WORKFLOWS = sorted((REPOSITORY_ROOT / ".github" / "workflows").glob("*.yml"))
# Workflows that act on the repository: cancelling one could leave its action half done.
ACTING = {"dependabot-auto-merge.yml", "issue-status.yml"}
GROUP = "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
CANCEL = "${{ github.event_name == 'pull_request' }}"


def _triggers(path: Path) -> set[str]:
    document = yaml.safe_load(path.read_text("utf-8"))
    found = document.get(True, document.get("on"))  # YAML reads a bare `on` key as True
    return (
        set(found) if isinstance(found, dict) else {found} if isinstance(found, str) else set(found)
    )


COVERED = [p for p in WORKFLOWS if "pull_request" in _triggers(p) and p.name not in ACTING]


@pytest.mark.parametrize("path", COVERED, ids=lambda p: p.name)
def test_a_pull_request_workflow_cancels_only_its_own_superseded_runs(path: Path) -> None:
    concurrency = yaml.safe_load(path.read_text("utf-8")).get("concurrency")
    assert concurrency == {"group": GROUP, "cancel-in-progress": CANCEL}, path.name


def test_only_the_workflows_that_act_are_left_out() -> None:
    acting = {p.name for p in WORKFLOWS if "pull_request" in _triggers(p)} - {
        p.name for p in COVERED
    }
    assert acting == ACTING
