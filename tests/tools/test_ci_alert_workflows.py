"""#428: the workflows and the CI alert listener's configuration agree, and the listener stays
isolated: least privilege, no untrusted checkout or artifact, every action pinned."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.repository import REPOSITORY_ROOT

WORKFLOWS = REPOSITORY_ROOT / ".github/workflows"
sys.path.insert(0, str(WORKFLOWS / "scripts"))
import alert_config  # noqa: E402

LISTENER = "ci-alerts.yml"


def workflow(path: Path) -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(path.read_text("utf-8"))
    loaded["on"] = loaded.pop(True, loaded.get("on")) or {}
    return loaded


def all_workflows() -> dict[str, dict[str, Any]]:
    return {path.name: workflow(path) for path in sorted(WORKFLOWS.glob("*.yml"))}


def watched_triggers(triggers: object) -> set[str]:
    """The triggers of a workflow that the listener must watch."""
    found: set[str] = set()
    if not isinstance(triggers, dict):
        return found
    push = triggers.get("push") or {}
    if "push" in triggers and (not push or "main" in (push.get("branches") or [])):
        found.add("push")
    if push and push.get("tags"):
        found.add("tag")
    found |= {event for event in ("schedule", "workflow_run") if event in triggers}
    return found


def missing_from(configuration: list[str], workflows: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        name
        for name, loaded in workflows.items()
        if name != LISTENER and watched_triggers(loaded["on"]) and name not in configuration
    )


def listener_findings(workflows: dict[str, dict[str, Any]], configuration: list[str]) -> list[str]:
    names = {loaded.get("name"): file for file, loaded in workflows.items()}
    listed = workflows[LISTENER]["on"]["workflow_run"]["workflows"]
    found = []
    if LISTENER in configuration or "ci-alerts" in listed:
        found.append("the listener watches itself")
    for name in listed:
        file = names.get(name)
        if file is None:
            found.append(f"{name}: no such workflow")
        elif "workflow_run" in workflows[file]["on"]:
            found.append(f"{name}: triggered by workflow_run, reconciled on the schedule only")
    return found


def configured() -> list[str]:
    return [watch.workflow for watch in alert_config.load()]


def test_every_workflow_with_a_watched_trigger_is_configured() -> None:
    assert missing_from(configured(), all_workflows()) == []


def test_a_new_watched_workflow_missing_from_the_configuration_fails() -> None:
    workflows = all_workflows() | {"new.yml": {"on": {"schedule": [{"cron": "1 1 * * *"}]}}}
    assert missing_from(configured(), workflows) == ["new.yml"]
    tagged = all_workflows() | {"tagged.yml": {"on": {"push": {"tags": ["v*"]}}}}
    assert missing_from(configured(), tagged) == ["tagged.yml"]


def test_the_listener_lists_neither_itself_nor_a_workflow_run_workflow() -> None:
    workflows = all_workflows()
    assert listener_findings(workflows, configured()) == []
    trigger = workflows[LISTENER]["on"]["workflow_run"]["workflows"]
    assert listener_findings(workflows, [*configured(), LISTENER]) == [
        "the listener watches itself"
    ]
    trigger.append("test-trends")
    assert listener_findings(workflows, configured()) == [
        "test-trends: triggered by workflow_run, reconciled on the schedule only"
    ]
    trigger[-1] = "ci-alerts"
    assert "the listener watches itself" in listener_findings(workflows, configured())


def test_every_configured_direct_workflow_triggers_the_listener() -> None:
    workflows = all_workflows()
    names = {file: loaded.get("name") for file, loaded in workflows.items()}
    listed = set(workflows[LISTENER]["on"]["workflow_run"]["workflows"])
    for watch in alert_config.load():
        if "workflow_run" not in watch.events:
            assert names[watch.workflow] in listed, watch.workflow


def issue_writers(workflows: dict[str, dict[str, Any]]) -> list[str]:
    found = []
    for file, loaded in workflows.items():
        scopes = [("", loaded.get("permissions"))] + [
            (job, spec.get("permissions")) for job, spec in loaded["jobs"].items()
        ]
        for job, permissions in scopes:
            writes = isinstance(permissions, dict) and permissions.get("issues") == "write"
            if writes or permissions == "write-all":
                found.append(f"{file}:{job}")
    return found


def test_only_the_listener_and_the_issue_automation_write_issues() -> None:
    # issue-status.yml and labels.yml (workflow-wide) keep their own issue and label automation.
    allowed = {f"{LISTENER}:ci-alerts", "issue-status.yml:", "labels.yml:"}
    assert set(issue_writers(all_workflows())) <= allowed
    planted = all_workflows()
    planted["python.yml"]["jobs"]["python-lint"]["permissions"] = {"issues": "write"}
    assert "python.yml:python-lint" in issue_writers(planted)


def test_the_listener_holds_exactly_its_two_permissions() -> None:
    loaded = workflow(WORKFLOWS / LISTENER)
    assert loaded["permissions"] == {}
    assert loaded["jobs"]["ci-alerts"]["permissions"] == {"actions": "read", "issues": "write"}
    assert loaded["concurrency"] == {"group": "ci-alerts", "cancel-in-progress": False}


def test_the_listener_checks_out_no_pull_request_and_downloads_no_artifact() -> None:
    steps = workflow(WORKFLOWS / LISTENER)["jobs"]["ci-alerts"]["steps"]
    text = (WORKFLOWS / LISTENER).read_text("utf-8")
    assert "download-artifact" not in text
    assert "gh run download" not in text
    for step in steps:
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            ref = str(step["with"]["ref"])
            assert not re.search(r"workflow_run|pull_request|head_sha|head_branch", ref), ref
    # No run data reaches a shell command: only fixed context values are in `env`.
    for step in steps:
        assert "${{" not in str(step.get("run", ""))


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_action_is_pinned_by_sha(name: str) -> None:
    for line in (WORKFLOWS / name).read_text("utf-8").splitlines():
        found = re.search(r"uses:\s*([^\s#]+)", line)
        if found and not found.group(1).startswith("./"):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", found.group(1)), line


def test_the_listener_starts_only_for_main_s_and_the_tags_runs() -> None:
    trigger = workflow(WORKFLOWS / LISTENER)["on"]["workflow_run"]
    assert trigger["branches"] == ["main", "v*"]


def test_the_health_check_runs_on_pull_requests_only() -> None:
    # On main it would fail the merge itself and report the listener back to the listener.
    job = workflow(WORKFLOWS / "checks.yml")["jobs"]["ci-alerts-healthy"]
    assert job["if"] == "github.event_name == 'pull_request'"


def test_the_ossie_report_is_kept_when_a_finding_fails_the_run() -> None:
    steps = workflow(WORKFLOWS / "ossie-upstream.yml")["jobs"]["ossie-upstream"]["steps"]
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert "!cancelled()" in upload["if"]


def test_test_trends_starts_a_new_history_only_when_none_was_kept() -> None:
    steps = workflow(WORKFLOWS / "test-trends.yml")["jobs"]["trends"]["steps"]
    fetch = next(s["run"] for s in steps if "Fetch" in s.get("name", ""))
    assert "/artifacts" in fetch
    assert 'if [ "$kept" != 0 ]; then' in fetch
    assert "&& gh run download" not in fetch  # a failed download is never a new history
