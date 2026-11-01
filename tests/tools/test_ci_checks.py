"""The canonical list of mandatory checks (tools/ci/checks.toml, #240) equals what CI runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.ci import checks
from tools.repository import REPOSITORY_ROOT


def test_the_list_equals_the_workflows_of_the_repository() -> None:
    assert checks.drift(REPOSITORY_ROOT) == []


def test_every_check_runs_locally_or_says_why_not() -> None:
    for check in checks.load(REPOSITORY_ROOT):
        assert check.local or check.reason, check.name


def write_repository(root: Path, workflow: str, listed: str) -> Path:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow)
    (root / "tools" / "ci").mkdir(parents=True)
    (root / "tools" / "ci" / "checks.toml").write_text(listed)
    return root


WORKFLOW = """name: ci
on: [pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: pip install x
      - run: ruff check .
"""
LISTED = f"""[[job]]
workflow = "ci.yml"
job = "lint"
kind = "check"
digest = "{checks.digest(yaml.safe_load(WORKFLOW)["jobs"]["lint"])}"
setup = ["pip install x"]
[[job.check]]
name = "ruff"
run = "ruff check ."
local = true
"""


def test_a_matching_sample_has_no_drift(tmp_path: Path) -> None:
    assert checks.drift(write_repository(tmp_path, WORKFLOW, LISTED)) == []


@pytest.mark.parametrize(
    ("workflow", "listed", "expected"),
    [
        (WORKFLOW + "      - run: mypy .\n", LISTED, "not listed"),
        (WORKFLOW.replace("      - run: ruff check .\n", ""), LISTED, "not in the workflow"),
        (
            WORKFLOW + "  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n",
            LISTED,
            "job ci.yml:test is not listed",
        ),
        (WORKFLOW, LISTED.replace("local = true", "local = false"), "needs a reason"),
        (
            WORKFLOW.replace(
                "      - run: ruff check .",
                "      - run: ruff check .\n        working-directory: web",
            ),
            LISTED,
            "directory",
        ),
        (
            WORKFLOW.replace("on: [pull_request]", "on:\n  pull_request:\n    paths: [x/**]"),
            LISTED,
            "scoped",
        ),
        (WORKFLOW, LISTED.replace('kind = "check"', 'kind = "scoped"'), "every pull request"),
        (
            WORKFLOW.replace(
                "    steps:", "    strategy:\n      matrix:\n        python: ['3.15']\n    steps:"
            ),
            LISTED,
            "pythons",
        ),
        (
            WORKFLOW,
            LISTED + '[[job]]\nworkflow = "ci.yml"\njob = "gone"\nkind = "automation"\n',
            "listed but not",
        ),
        (
            WORKFLOW,
            LISTED.replace('setup = ["pip install x"]', 'setup = ["pip install x", "make"]'),
            "setup step",
        ),
    ],
)
def test_any_difference_is_reported(
    tmp_path: Path, workflow: str, listed: str, expected: str
) -> None:
    found = checks.drift(write_repository(tmp_path, workflow, listed))
    assert any(expected in line for line in found), found


# --- Required checks cannot be skipped silently (#267) -------------------------------------

CHECKOUT = {"uses": "actions/checkout@v7"}


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f'"{k}" = {toml_value(v)}' for k, v in value.items()) + " }"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sample(tmp_path: Path, workflow: dict[str, Any], entries: dict[str, Any]) -> list[str]:
    """Write one workflow and its list (digests computed) and return the drift findings."""
    text = yaml.safe_dump(workflow, sort_keys=False)
    parsed = yaml.safe_load(text)
    lines = []
    for job, entry in entries.items():
        lines += ["[[job]]", 'workflow = "ci.yml"', f'job = "{job}"']
        kind = entry.get("kind", "check")
        lines.append(f'kind = "{kind}"')
        if kind in ("check", "scoped"):
            lines.append(f'digest = "{checks.digest(parsed["jobs"][job])}"')
        for key, value in entry.items():
            if key not in ("kind", "check"):
                lines.append(f"{key} = {toml_value(value)}")
        lines.append("setup = []")
        for check in entry.get("check", []):
            lines.append("[[job.check]]")
            lines += [f"{k} = {toml_value(v)}" for k, v in check.items()]
    root = tmp_path / f"repository-{sum(1 for _ in tmp_path.iterdir())}"
    return checks.drift(write_repository(root, text, "\n".join(lines) + "\n"))


def step(run: str, **extra: object) -> dict[str, Any]:
    return {"run": run, **extra}


def required_job(**extra: object) -> dict[str, Any]:
    return {"runs-on": "ubuntu-latest", "steps": [CHECKOUT, step("ruff check .")], **extra}


RUFF = {"name": "ruff", "run": "ruff check .", "local": True}
REQUIRED = {"required": True, "contexts": ["lint"], "check": [RUFF]}


def workflow_of(jobs: dict[str, Any], on: object = None, **extra: object) -> dict[str, Any]:
    return {
        "name": "ci",
        "on": on if on is not None else {"pull_request": None},
        "permissions": {"contents": "read"},
        "jobs": jobs,
        **extra,
    }


def test_a_plain_required_job_passes(tmp_path: Path) -> None:
    assert sample(tmp_path, workflow_of({"lint": required_job()}), {"lint": REQUIRED}) == []


@pytest.mark.parametrize(
    ("job", "on", "expected"),
    [
        # A1, A3: job-level `if` and `continue-on-error`.
        (required_job(**{"if": "github.actor != 'x'"}), None, "can be skipped"),
        (required_job(**{"continue-on-error": True}), None, "continue-on-error"),
        # A4, A5: on a check step.
        (
            required_job(steps=[CHECKOUT, step("ruff check .", **{"continue-on-error": True})]),
            None,
            "continue-on-error",
        ),
        (
            required_job(steps=[CHECKOUT, step("ruff check .", **{"if": "false"})]),
            None,
            "can be skipped by `if: false`",
        ),
        # A6-A8: triggers that can leave a required check out.
        (required_job(), {"pull_request": {"paths": ["src/**"]}}, "pull_request.paths"),
        (
            required_job(),
            {"pull_request": {"paths-ignore": ["docs/**"]}},
            "pull_request.paths-ignore",
        ),
        (required_job(), {"pull_request": {"branches": ["main"]}}, "pull_request.branches"),
        (required_job(), {"pull_request_target": {"branches-ignore": ["x"]}}, "branches-ignore"),
        (required_job(), {"pull_request": {"types": ["opened", "reopened"]}}, "types"),
        # A9: a matrix exclude.
        (
            required_job(
                strategy={"matrix": {"python": ["3.15", "3.14"], "exclude": [{"python": "3.14"}]}}
            ),
            None,
            "exclude",
        ),
        # A10: the name does not produce the listed check name.
        (required_job(name="lint-renamed"), None, "produces check names ['lint-renamed']"),
        (required_job(name="lint-${{ github.ref }}"), None, "produces check names None"),
    ],
)
def test_every_way_to_skip_a_required_check_is_refused(
    tmp_path: Path, job: dict[str, Any], on: object, expected: str
) -> None:
    entry = dict(REQUIRED)
    if "strategy" in job:
        entry["pythons"] = ["3.15", "3.14"]
    found = sample(tmp_path, workflow_of({"lint": job}, on=on), {"lint": entry})
    assert any(expected in line for line in found), found


def test_a_required_job_whose_workflow_does_not_run_on_pull_requests_is_refused(
    tmp_path: Path,
) -> None:
    # A11: the job is not seen as a pull-request job, so its listing is reported.
    found = sample(
        tmp_path, workflow_of({"lint": required_job()}, on={"push": None}), {"lint": REQUIRED}
    )
    assert any("listed but not in the workflows" in line for line in found), found
    assert any("must come from ci.yml:lint alone" in line for line in found), found


def test_a_required_need_outside_an_aggregator_is_refused(tmp_path: Path) -> None:
    # A2.
    jobs = {"build": required_job(**{"if": "false"}), "lint": required_job(needs="build")}
    entries = {"build": {"check": [RUFF]}, "lint": REQUIRED}
    found = sample(tmp_path, workflow_of(jobs), entries)
    assert any("ci.yml:lint is required and can be skipped" in line for line in found), found


def test_a_check_name_produced_twice_is_refused(tmp_path: Path) -> None:
    jobs = {"lint": required_job(), "other": required_job(name="lint")}
    found = sample(tmp_path, workflow_of(jobs), {"lint": REQUIRED, "other": {"check": [RUFF]}})
    assert any(
        "pull-request jobs producing it: ci.yml:lint, ci.yml:other" in line for line in found
    ), found


def aggregator(lines: list[str], env: dict[str, str], needs: list[str]) -> dict[str, Any]:
    return {
        "name": "tests",
        "needs": needs,
        "if": "always()",
        "runs-on": "ubuntu-latest",
        "steps": [step("\n".join(lines), env=env)],
    }


PARTS = {"tests-part": required_job()}
PART_ENTRY = {"check": [RUFF]}
AGGREGATED = {"required": True, "contexts": ["tests"], "check": []}


def aggregated_findings(
    tmp_path: Path, job: dict[str, Any], extra: dict[str, Any] | None = None
) -> list[str]:
    jobs = {**PARTS, **(extra or {}), "tests": job}
    entries: dict[str, Any] = {"tests-part": PART_ENTRY, "tests": AGGREGATED}
    if extra:
        entries.update(dict.fromkeys(extra, PART_ENTRY))
    workflow = workflow_of(jobs)
    # The aggregator's step is its check.
    run_line = str(job["steps"][0]["run"]).splitlines()[0]
    entries["tests"] = {
        **AGGREGATED,
        "check": [{"name": "aggregate", "run": run_line, "local": False, "reason": "CI"}],
    }
    return sample(tmp_path, workflow, entries)


def test_an_aggregator_that_tests_every_need_for_success_is_safe(tmp_path: Path) -> None:
    # S1: the form of python-tests.
    job = aggregator(
        ['test "$RESULT" = success'], {"RESULT": "${{ needs.tests-part.result }}"}, ["tests-part"]
    )
    assert aggregated_findings(tmp_path, job) == []


@pytest.mark.parametrize(
    ("lines", "env", "needs", "expected"),
    [
        # S2: a need that is not tested.
        (
            ['test "$A" = success'],
            {"A": "${{ needs.tests-part.result }}"},
            ["tests-part", "other"],
            "other are not tested",
        ),
        # S3, S4: skipped, failure or cancelled accepted.
        (
            ['test "$A" != failure'],
            {"A": "${{ needs.tests-part.result }}"},
            ["tests-part"],
            "is not a test that a needed job succeeded",
        ),
        (
            ['test "$A" = success || test "$A" = skipped'],
            {"A": "${{ needs.tests-part.result }}"},
            ["tests-part"],
            "is not a test",
        ),
        (
            ['test "$A" = success', "true"],
            {"A": "${{ needs.tests-part.result }}"},
            ["tests-part"],
            "`true`",
        ),
        # A variable bound to something else than the need's result.
        (['test "$A" = success'], {"A": "success"}, ["tests-part"], "is not a test"),
    ],
)
def test_a_broken_aggregator_is_refused(
    tmp_path: Path, lines: list[str], env: dict[str, str], needs: list[str], expected: str
) -> None:
    extra = {"other": required_job()} if "other" in needs else None
    found = aggregated_findings(tmp_path, aggregator(lines, env, needs), extra)
    assert any("not a safe aggregator" in line and expected in line for line in found), found


def test_report_steps_on_failure_and_include_only_matrices_are_safe(tmp_path: Path) -> None:
    # S5, S6, S7.
    job = required_job(
        name="lint-${{ matrix.python }}",
        strategy={
            "matrix": {"python": ["3.15", "3.14"], "include": [{"python": "3.15", "lock": "a"}]}
        },
        steps=[CHECKOUT, step("ruff check ."), step("report", **{"if": "failure()"})],
    )
    entry = {
        "required": True,
        "contexts": ["lint-3.15", "lint-3.14"],
        "pythons": ["3.15", "3.14"],
        "check": [RUFF, {"name": "report", "run": "report", "local": False, "reason": "CI"}],
    }
    on = {
        "pull_request_target": {"types": ["opened", "edited", "synchronize", "reopened", "labeled"]}
    }
    assert sample(tmp_path, workflow_of({"lint": job}, on=on), {"lint": entry}) == []


def test_a_condition_on_a_job_nothing_required_needs_is_allowed(tmp_path: Path) -> None:
    # S8.
    jobs = {
        "lint": required_job(),
        "bot": {"if": "github.actor == 'bot'", "runs-on": "x", "steps": [step("act")]},
    }
    assert (
        sample(tmp_path, workflow_of(jobs), {"lint": REQUIRED, "bot": {"kind": "automation"}}) == []
    )


def test_a_listed_step_condition_is_allowed_in_a_required_job(tmp_path: Path) -> None:
    job = required_job(
        steps=[CHECKOUT, step("ruff check .", **{"if": "startsWith(github.ref, 'refs/tags/v')"})]
    )
    entry = {**REQUIRED, "conditions": ["startsWith(github.ref, 'refs/tags/v')"]}
    assert sample(tmp_path, workflow_of({"lint": job}), {"lint": entry}) == []


def scoped_workflow(**changes: object) -> dict[str, Any]:
    gate = {
        "name": "ci-changes",
        "runs-on": "x",
        "outputs": {"relevant": "${{ steps.c.outputs.relevant }}"},
        "steps": [CHECKOUT, step("python3 .github/workflows/scripts/changes.py", id="c")],
    }
    real = {
        "needs": "changes",
        "if": checks.SCOPED_CONDITION,
        "runs-on": "x",
        "steps": [step("docker build .")],
    }
    report = {
        "needs": ["changes", "build"],
        "if": "always()",
        "runs-on": "x",
        "steps": [
            CHECKOUT,
            step(
                checks.AGGREGATE,
                env={
                    "CHECK": "docker",
                    "CHANGES": "${{ needs.changes.result }}",
                    "RELEVANT": "${{ needs.changes.outputs.relevant }}",
                    "RESULT": "${{ needs.build.result }}",
                },
            ),
        ],
    }
    jobs: dict[str, Any] = {"changes": gate, "build": real, "docker": report}
    for name, body in changes.items():
        jobs[name] = body
    return workflow_of(jobs, env={"RELEVANT_PATHS": "Dockerfile\n"})


SCOPED: dict[str, dict[str, Any]] = {
    "changes": {
        "contexts": ["ci-changes"],
        "check": [
            {
                "name": "changes",
                "run": "python3 .github/workflows/scripts/changes.py",
                "local": False,
                "reason": "CI",
            }
        ],
    },
    "build": {"kind": "scoped", "reason": "CI", "paths": ["Dockerfile"]},
    "docker": {
        "contexts": ["docker"],
        "check": [{"name": "docker", "run": checks.AGGREGATE, "local": False, "reason": "CI"}],
    },
}


def test_a_gated_scoped_check_with_its_aggregator_passes(tmp_path: Path) -> None:
    assert sample(tmp_path, scoped_workflow(), SCOPED) == []
    required = {**SCOPED, "docker": {**SCOPED["docker"], "required": True}}
    assert sample(tmp_path, scoped_workflow(), required) == []


def test_the_scoped_check_s_paths_must_equal_the_workflow_s(tmp_path: Path) -> None:
    # B8.
    entries = {**SCOPED, "build": {**SCOPED["build"], "paths": ["Dockerfile", "x"]}}
    found = sample(tmp_path, scoped_workflow(), entries)
    assert any("differ from RELEVANT_PATHS" in line for line in found), found


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {
                "CHANGES": "success",
                "RELEVANT": "${{ needs.changes.outputs.relevant }}",
                "RESULT": "${{ needs.build.result }}",
            },
            "CHANGES",
        ),
        (
            {
                "CHANGES": "${{ needs.changes.result }}",
                "RELEVANT": "true",
                "RESULT": "${{ needs.build.result }}",
            },
            "RELEVANT",
        ),
        (
            {
                "CHANGES": "${{ needs.changes.result }}",
                "RELEVANT": "${{ needs.changes.outputs.relevant }}",
                "RESULT": "success",
            },
            "RESULT",
        ),
    ],
)
def test_an_aggregator_script_not_bound_to_its_needs_is_refused(
    tmp_path: Path, env: dict[str, str], expected: str
) -> None:
    workflow = scoped_workflow()
    workflow["jobs"]["docker"]["steps"][1]["env"] = env
    found = sample(tmp_path, workflow, SCOPED)
    assert any("not a safe aggregator" in line and expected in line for line in found), found


def test_a_scoped_check_without_its_gate_or_aggregator_is_refused(tmp_path: Path) -> None:
    ungated = scoped_workflow(build={"runs-on": "x", "steps": [step("docker build .")]})
    found = sample(tmp_path, ungated, SCOPED)
    assert any("not gated by its workflow's changes job" in line for line in found), found
    alone = scoped_workflow()
    del alone["jobs"]["docker"]
    entries = {k: v for k, v in SCOPED.items() if k != "docker"}
    found = sample(tmp_path, alone, entries)
    assert any("no aggregator reports it" in line for line in found), found


def test_permissions_cannot_widen(tmp_path: Path) -> None:
    # B9: a scoped check's workflow stays read-only; a job's write permissions are listed.
    widened = scoped_workflow()
    widened["permissions"] = {"contents": "write"}
    found = sample(tmp_path, widened, SCOPED)
    assert any("a scoped check's workflow has permissions" in line for line in found), found
    assert any("are not read-only; list them" in line for line in found), found
    job = required_job(permissions={"contents": "read", "issues": "write"})
    found = sample(tmp_path, workflow_of({"lint": job}), {"lint": REQUIRED})
    assert any("are not read-only" in line for line in found), found
    listed = {**REQUIRED, "permissions": {"contents": "read", "issues": "write"}}
    assert sample(tmp_path, workflow_of({"lint": job}), {"lint": listed}) == []
