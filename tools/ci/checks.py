"""The canonical list of mandatory checks (`tools/ci/checks.toml`, #240) and its drift from CI.

`load` reads the list; `drift` compares it with the workflows and returns one line per
difference, so the list cannot fall behind what pull requests run.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sys
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
# A required check must run on every update of a pull request.
PULL_REQUEST_TYPES = frozenset({"opened", "synchronize", "reopened"})
TRIGGER_FILTERS = ("paths", "paths-ignore", "branches", "branches-ignore")
# Step conditions that never skip a step on a run that can fail: they only add runs.
SAFE_STEP_CONDITIONS = frozenset({"failure()", "always()"})
SCOPED_CONDITION = "needs.changes.outputs.relevant == 'true'"
AGGREGATE = "python3 .github/workflows/scripts/aggregate.py"
CHANGES = "changes"
_NEEDS_RESULT = re.compile(r"^\$\{\{ needs\.([A-Za-z0-9_-]+)\.result \}\}$")
_TEST_SUCCESS = re.compile(r'^test "\$([A-Za-z_][A-Za-z0-9_]*)" = success$')
_MATRIX_NAME = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
# Actions a check job may use: they prepare the runner and check nothing themselves. Any
# other action in a check job is drift, so a gate cannot be added as a `uses:` step unseen.
PREPARING_ACTIONS = (
    "actions/checkout",
    "actions/setup-python",
    "actions/setup-node",
    "actions/upload-artifact",
    "actions/download-artifact",
)


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


def digest(body: dict[str, Any]) -> str:
    """A digest of the whole job as the workflow defines it: every step's full text, its
    environment, conditions, services and matrix. Any change to the job changes it."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def drift(root: Path = REPOSITORY_ROOT) -> list[str]:
    """One line per difference between the list and the pull-request workflows."""
    found: list[str] = []
    listed = {(job["workflow"], job["job"]): job for job in _jobs(root)}
    seen: set[tuple[str, str]] = set()
    producers: dict[str, list[str]] = {}
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
        found.extend(_skips(path.name, workflow, events, listed))
        for name, body in workflow.get("jobs", {}).items():
            for context in _contexts(body, name) or []:
                producers.setdefault(context, []).append(f"{path.name}:{name}")
    for workflow_name, job_name in sorted(set(listed) - seen):
        found.append(f"job {workflow_name}:{job_name} is listed but not in the workflows")
    for (workflow_name, job_name), job in sorted(listed.items()):
        for context in job.get("contexts", []):
            jobs = producers.get(context, [])
            if jobs != [f"{workflow_name}:{job_name}"]:
                found.append(
                    f"check name {context} must come from {workflow_name}:{job_name} alone; "
                    f"pull-request jobs producing it: {', '.join(jobs) or 'none'}"
                )
    return found


def _needs(body: dict[str, Any]) -> list[str]:
    needs = body.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _contexts(body: dict[str, Any], name: str) -> list[str] | None:
    """The status-check names a job produces, or None when they cannot be computed."""
    template = str(body.get("name", name))
    keys = _MATRIX_NAME.findall(template)
    if "${{" in _MATRIX_NAME.sub("", template):
        return None
    if not keys:
        return [template]
    matrix = body.get("strategy", {}).get("matrix", {})
    if not all(isinstance(matrix.get(key), list) for key in keys):
        return None
    names = []
    for values in itertools.product(*(matrix[key] for key in keys)):
        name_of = template
        for key, value in zip(keys, values, strict=True):
            name_of = re.sub(
                r"\$\{\{\s*matrix\." + re.escape(key) + r"\s*\}\}", str(value), name_of
            )
        names.append(name_of)
    return names


def _aggregator(body: dict[str, Any], jobs: dict[str, Any], listed: dict[str, Any]) -> str | None:
    """Why a job with `if: always()` and `needs` is not a safe aggregator, or None if it is.

    Safe forms: every step tests `test "$X" = success` for a variable bound to each needed
    job's result; or the scoped-check aggregator script with the `changes` job's result and
    output and its one scoped job's result.
    """
    needs = set(_needs(body))
    if body.get("if") != "always()" or not needs:
        return "an aggregator must have `if: always()` and `needs`"
    runs = [step for step in body.get("steps", []) if "run" in step]
    if not runs or any(
        "if" in step or "continue-on-error" in step for step in body.get("steps", [])
    ):
        return "an aggregator's steps must run unconditionally"
    if len(runs) == 1 and str(runs[0]["run"]).strip() == AGGREGATE:
        env = runs[0].get("env", {})
        scoped = needs - {CHANGES}
        if CHANGES not in needs or len(scoped) != 1:
            return f"the aggregator script needs `{CHANGES}` and one scoped job"
        (real,) = scoped
        if listed.get(real, {}).get("kind") != "scoped":
            return f"{real} is not listed as scoped"
        expected = {
            "CHANGES": f"${{{{ needs.{CHANGES}.result }}}}",
            "RELEVANT": f"${{{{ needs.{CHANGES}.outputs.relevant }}}}",
            "RESULT": f"${{{{ needs.{real}.result }}}}",
        }
        wrong = [key for key, value in expected.items() if env.get(key) != value]
        return (
            f"the aggregator script's {', '.join(wrong)} are not bound to its needs"
            if wrong
            else None
        )
    tested: set[str] = set()
    for step in runs:
        variables = {
            key: match.group(1)
            for key, value in step.get("env", {}).items()
            if (match := _NEEDS_RESULT.match(str(value)))
        }
        for line in str(step["run"]).strip().splitlines():
            match = _TEST_SUCCESS.match(line.strip())
            if match is None or match.group(1) not in variables:
                return f"line `{line.strip()}` is not a test that a needed job succeeded"
            tested.add(variables[match.group(1)])
    if missing := sorted(needs - tested):
        return f"needs {', '.join(missing)} are not tested for success"
    return None


def _protected(required: set[str], jobs: dict[str, Any]) -> set[str]:
    """The required jobs and every job they need, directly or not."""
    protected: set[str] = set()
    pending = list(required)
    while pending:
        name = pending.pop()
        if name in protected or name not in jobs:
            continue
        protected.add(name)
        pending.extend(_needs(jobs[name]))
    return protected


def _skips(
    workflow_name: str,
    workflow: dict[str, Any],
    events: dict[str, Any],
    listed: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """Every way the workflow could skip a required check, or widen a job's permissions."""
    found: list[str] = []
    jobs: dict[str, Any] = workflow.get("jobs", {})
    here = {job: entry for (wf, job), entry in listed.items() if wf == workflow_name}
    required = {job for job, entry in here.items() if entry.get("required")}
    top = workflow.get("permissions")
    for name, body in jobs.items():
        entry = here.get(name)
        if entry is None:
            continue
        label = f"{workflow_name}:{name}"
        permissions = body.get("permissions", top)
        allowed = entry.get("permissions")
        if allowed is not None and permissions != allowed:
            found.append(f"job {label}: permissions {permissions} differ from the listed {allowed}")
        if allowed is None and any(v not in ("read", "none") for v in (permissions or {}).values()):
            found.append(f"job {label}: permissions {permissions} are not read-only; list them")
        aggregates = body.get("if") == "always()" and _needs(body) and entry.get("kind") == "check"
        if aggregates and (why := _aggregator(body, jobs, here)) is not None:
            found.append(f"job {label}: not a safe aggregator: {why}")
        if (contexts := entry.get("contexts")) is not None and _contexts(body, name) != contexts:
            found.append(
                f"job {label}: produces check names {_contexts(body, name)}, listed {contexts}"
            )
        if entry.get("required") and not entry.get("contexts"):
            found.append(f"job {label} is required and lists no check names")
    if any(here[job].get("kind") == "scoped" for job in here):
        if top != {"contents": "read"}:
            found.append(
                f"workflow {workflow_name}: a scoped check's workflow has permissions {top}"
            )
        paths = [
            line.strip()
            for line in str(workflow.get("env", {}).get("RELEVANT_PATHS", "")).splitlines()
            if line.strip()
        ]
        for job, entry in here.items():
            if entry.get("kind") == "scoped" and entry.get("paths") != paths:
                found.append(
                    f"job {workflow_name}:{job}: paths {entry.get('paths')} "
                    f"differ from RELEVANT_PATHS {paths}"
                )
            if entry.get("kind") == "scoped" and not any(
                str(step.get("run", "")).strip() == AGGREGATE
                and step.get("env", {}).get("RESULT") == f"${{{{ needs.{job}.result }}}}"
                for body in jobs.values()
                for step in body.get("steps", [])
            ):
                found.append(f"job {workflow_name}:{job} is scoped and no aggregator reports it")
    if not required:
        return found
    for event in PULL_REQUEST_EVENTS:
        if event not in events:
            continue
        trigger = events[event] or {}
        for key in TRIGGER_FILTERS:
            if key in trigger:
                found.append(
                    f"workflow {workflow_name}: `{event}.{key}` can leave out its required checks"
                )
        if "types" in trigger and not set(trigger["types"]) >= PULL_REQUEST_TYPES:
            found.append(
                f"workflow {workflow_name}: `{event}.types` must include "
                f"{', '.join(sorted(PULL_REQUEST_TYPES))}"
            )
    for name in sorted(required):
        body = jobs.get(name)
        if body is None:
            continue
        label = f"{workflow_name}:{name}"
        if ("if" in body or _needs(body)) and _aggregator(body, jobs, here) is not None:
            found.append(
                f"job {label} is required and can be skipped: "
                "`if` or `needs` outside a safe aggregator"
            )
    for name in sorted(_protected(required, jobs)):
        body = jobs[name]
        label = f"{workflow_name}:{name}"
        conditions = set(here.get(name, {}).get("conditions", []))
        if "continue-on-error" in body:
            found.append(f"job {label}: `continue-on-error` hides a failure of a required check")
        for step in body.get("steps", []):
            title = step.get("name") or step.get("run", step.get("uses", "?"))
            title = str(title).splitlines()[0]
            if "continue-on-error" in step:
                found.append(f"job {label}: step `{title}` has `continue-on-error`")
            condition = step.get("if")
            if condition is not None and condition not in SAFE_STEP_CONDITIONS | conditions:
                found.append(f"job {label}: step `{title}` can be skipped by `if: {condition}`")
        matrix = body.get("strategy", {}).get("matrix", {})
        if "exclude" in matrix:
            found.append(f"job {label}: `strategy.matrix.exclude` can remove a required run")
    return found


def _job_drift(label: str, job: dict[str, Any], body: dict[str, Any], *, scoped: bool) -> list[str]:
    found: list[str] = []
    kind = job.get("kind")
    if kind not in KINDS:
        return [f"job {label}: kind must be one of {', '.join(KINDS)}"]
    if scoped and kind != "automation":
        found.append(
            f"job {label} runs only on some paths: make it scoped, gated by a changes job "
            "and reported by an aggregator"
        )
    if kind == "scoped" and not job.get("reason"):
        found.append(f"job {label} is scoped and needs a reason")
    if kind == "scoped" and (body.get("if") != SCOPED_CONDITION or CHANGES not in _needs(body)):
        found.append(
            f"job {label} is scoped but is not gated by its workflow's changes job "
            f"(`needs: {CHANGES}` and `if: {SCOPED_CONDITION}`), so it runs on every pull request"
        )
    if kind == "automation":
        return found
    if job.get("digest") != (current := digest(body)):
        found.append(
            f"job {label} changed since this list was checked against it: compare every step "
            f'with its entries here, then set digest = "{current}"'
        )
    if kind == "scoped":
        return found
    for step in body.get("steps", []):
        action = str(step.get("uses", "")).split("@")[0]
        if action and action not in PREPARING_ACTIONS:
            found.append(f"job {label}: action {action} is not a preparing action; list it")
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


def main() -> int:
    found = drift()
    print("\n".join(found) if found else f"{LIST}: matches the workflows")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
