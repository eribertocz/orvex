"""The canonical list of mandatory checks (`tools/ci/checks.toml`, #240) and its drift from CI.

`load` reads the list; `drift` compares it with the workflows and returns one line per
difference, so the list cannot fall behind what pull requests run.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.repository import REPOSITORY_ROOT

LIST = Path("tools/ci/checks.toml")
WORKFLOWS = Path(".github/workflows")
KINDS = ("check", "scoped", "automation")
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")


@dataclass(frozen=True)
class Check:
    workflow: str
    job: str
    name: str
    run: str
    directory: str | None
    local: bool
    reason: str | None
    pythons: tuple[str, ...]


def _jobs(root: Path) -> list[dict[str, Any]]:
    jobs = tomllib.loads((root / LIST).read_text(encoding="utf-8")).get("job", [])
    if not isinstance(jobs, list):
        raise ValueError(f"{LIST}: `job` must be an array of tables")
    return jobs


def load(root: Path = REPOSITORY_ROOT) -> list[Check]:
    """Every listed check, in list order."""
    return [
        Check(
            workflow=job["workflow"],
            job=job["job"],
            name=check["name"],
            run=check["run"],
            directory=check.get("directory"),
            local=bool(check.get("local", False)),
            reason=check.get("reason"),
            pythons=tuple(job.get("pythons", ())),
        )
        for job in _jobs(root)
        for check in job.get("check", [])
    ]


def _events(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean true.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return dict(on or {})


def _first_line(step: dict[str, Any]) -> str:
    return str(step["run"]).splitlines()[0]


def drift(root: Path = REPOSITORY_ROOT) -> list[str]:
    """One line per difference between the list and the pull-request workflows."""
    found: list[str] = []
    listed = {(job["workflow"], job["job"]): job for job in _jobs(root)}
    seen: set[tuple[str, str]] = set()
    for path in sorted((root / WORKFLOWS).glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        events = _events(workflow)
        if not any(event in events for event in PULL_REQUEST_EVENTS):
            continue
        scoped = any(
            isinstance(events.get(event), dict) and events[event].get("paths")
            for event in PULL_REQUEST_EVENTS
        )
        for name, body in workflow.get("jobs", {}).items():
            key = (path.name, name)
            label = f"{path.name}:{name}"
            if key not in listed:
                found.append(f"job {label} is not listed in {LIST}")
                continue
            seen.add(key)
            found.extend(_job_drift(label, listed[key], body, scoped=scoped))
    for workflow_name, job_name in sorted(set(listed) - seen):
        found.append(f"job {workflow_name}:{job_name} is listed but not in the workflows")
    return found


def _job_drift(label: str, job: dict[str, Any], body: dict[str, Any], *, scoped: bool) -> list[str]:
    found: list[str] = []
    kind = job.get("kind")
    if kind not in KINDS:
        return [f"job {label}: kind must be one of {', '.join(KINDS)}"]
    if kind == "scoped" and not scoped:
        found.append(f"job {label} is scoped but its workflow runs on every pull request")
    if kind == "check" and scoped:
        found.append(f"job {label} runs only on some paths; list it as scoped")
    if kind == "scoped" and not job.get("reason"):
        found.append(f"job {label} is scoped and needs a reason")
    if kind != "check":
        return found
    matrix = body.get("strategy", {}).get("matrix", {}).get("python")
    if tuple(matrix or ()) != tuple(job.get("pythons", ())):
        found.append(f"job {label}: pythons {job.get('pythons', [])} differ from matrix {matrix}")
    default = body.get("defaults", {}).get("run", {}).get("working-directory")
    steps = [s for s in body.get("steps", []) if "run" in s]
    setup = list(job.get("setup", []))
    checks = list(job.get("check", []))
    for step in steps:
        line = _first_line(step)
        directory = step.get("working-directory", default)
        if line in setup:
            setup.remove(line)
            continue
        match = next((c for c in checks if c["run"] == line), None)
        if match is None:
            found.append(f"job {label}: step `{line}` is not listed")
            continue
        checks.remove(match)
        if match.get("directory") != directory:
            found.append(
                f"job {label}: check {match['name']} directory {match.get('directory')!r} "
                f"differs from the workflow's {directory!r}"
            )
        if not match.get("local") and not match.get("reason"):
            found.append(f"job {label}: check {match['name']} runs only in CI and needs a reason")
    for line in setup:
        found.append(f"job {label}: setup step `{line}` is not in the workflow")
    for check in checks:
        found.append(f"job {label}: check {check['name']} is not in the workflow")
    return found


def script(root: Path, check: Check) -> str:
    """The whole `run` text of the workflow step a check names."""
    workflow = yaml.safe_load((root / WORKFLOWS / check.workflow).read_text(encoding="utf-8"))
    for step in workflow["jobs"][check.job].get("steps", []):
        if "run" in step and _first_line(step) == check.run:
            return str(step["run"])
    raise ValueError(f"check {check.name}: no step `{check.run}` in {check.workflow}")


def not_run_jobs(root: Path = REPOSITORY_ROOT) -> list[tuple[str, str]]:
    """The scoped jobs, which the local verification does not run, with their reasons."""
    return [
        (f"{job['workflow']}:{job['job']}", str(job.get("reason", "")))
        for job in _jobs(root)
        if job.get("kind") == "scoped"
    ]
