"""The weekly read-only watch of apache/ossie (#285, ADR-0030 §7), offline: each fake upstream
is a local Git repository built by the test, and each proposal answer is a fixture."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.ci.protection import listed_required
from tools.ossie import watch
from tools.repository import REPOSITORY_ROOT

# The fake upstreams are built with this reference, so `git_calls` records the watch's calls only.
RUN = subprocess.run
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ossie-upstream.yml"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {"type": "string", "const": "0.2.0.dev0"},
        "name": {"type": "string"},
        "metrics": {"type": "array"},
    },
    "required": ["version", "name"],
    "additionalProperties": False,
}
SPEC = """# Core Metadata Specification

## Goals

Describe models.

```yaml
# Not a heading: a comment in a code block
```

## Metrics

A metric aggregates a field.

## Version History

- 0.2.0.dev0
"""
FILES = {
    "core-spec/ossie-schema.json": json.dumps(SCHEMA, indent=2) + "\n",
    "core-spec/spec.md": SPEC,
    "core-spec/expression_language.md": "# Expressions\n\nSUM(x) skips NULL.\n",
    "core-spec/README.md": "# Core\n\nRead the spec.\n",
    "extensions/dialects.md": "| Token |\n|---|\n| ANSI_SQL |\n| DAX |\n",
    "README.md": "# Upstream\n",
    "examples/model.yaml": "name: demo\n",
}
VENDORED = {
    "ossie-schema.json": "core-spec/ossie-schema.json",
    "spec.md": "core-spec/spec.md",
    "expression_language.md": "core-spec/expression_language.md",
}


def git(repository: Path, *args: str) -> str:
    return RUN(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class Upstream:
    """A fake upstream: its first commit is the pinned one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        git(root, "init", "--quiet", "--initial-branch=main")
        git(root, "config", "user.name", "Upstream")
        git(root, "config", "user.email", "upstream@example.invalid")
        git(root, "config", "commit.gpgsign", "false")
        self.pinned = self.commit("initial", FILES)

    def commit(self, subject: str, files: Mapping[str, str | None]) -> str:
        for path, content in files.items():
            target = self.root / path
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git(self.root, "add", "--all")
        git(self.root, "commit", "--quiet", "-m", subject)
        return git(self.root, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path / "upstream")


def write_inputs(
    tmp_path: Path, upstream: Upstream, proposals: dict[int, str] | None = None
) -> tuple[Path, Path]:
    """A watch list and a registry entry (PIN.toml) for `upstream`."""
    watch_list = tmp_path / "watch.toml"
    recorded = "\n".join(f'{number} = "{state}"' for number, state in (proposals or {}).items())
    watch_list.write_text(
        f"repository = {json.dumps(str(upstream.root))}\n"
        'api = "https://api.github.com/repos/apache/ossie"\n'
        'branch = "main"\n'
        'paths = ["core-spec/", "extensions/dialects.md", "ROADMAP.md"]\n'
        f"[proposals]\n{recorded}\n",
        encoding="utf-8",
    )
    hashes = "\n".join(
        f'"{name}" = "{hashlib.sha256(FILES[path].encode()).hexdigest()}"'
        for name, path in VENDORED.items()
    )
    files = "\n".join(f'"{name}" = "{path}"' for name, path in VENDORED.items())
    pin = tmp_path / "PIN.toml"
    pin.write_text(
        f'commit = "{upstream.pinned}"\nid = "fake"\n[files]\n{files}\n[sha256]\n{hashes}\n',
        encoding="utf-8",
    )
    return watch_list, pin


def no_proposal(number: int) -> dict[str, Any]:
    raise AssertionError(f"proposal {number} was not expected to be read")


def report_of(tmp_path: Path, upstream: Upstream, fetch: watch.Fetch = no_proposal) -> watch.Report:
    watch_list, pin = write_inputs(tmp_path, upstream)
    return watch.run(watch.load_watch_list(watch_list), watch.load_pin(pin), fetch)


def change_class(report: watch.Report, path: str) -> str:
    (change,) = [change for change in report.changes if change.path == path]
    return change.kind


def schema_with(edit: Callable[[dict[str, Any]], None]) -> str:
    schema = json.loads(json.dumps(SCHEMA))
    edit(schema)
    return json.dumps(schema, indent=2) + "\n"


# --- Classes -----------------------------------------------------------------------------------


def test_a_readme_edit_is_informational(tmp_path: Path, upstream: Upstream) -> None:
    upstream.commit("docs: reword", {"core-spec/README.md": "# Core\n\nRead it all.\n"})
    assert change_class(report_of(tmp_path, upstream), "core-spec/README.md") == "informational"


def test_a_new_optional_schema_property_is_compatible(tmp_path: Path, upstream: Upstream) -> None:
    new = schema_with(lambda schema: schema["properties"].update(owner={"type": "string"}))
    upstream.commit("feat: owner", {"core-spec/ossie-schema.json": new})
    assert change_class(report_of(tmp_path, upstream), "core-spec/ossie-schema.json") == (
        "compatible"
    )


@pytest.mark.parametrize(
    ("name", "edit", "reason"),
    [
        ("removed property", lambda schema: schema["properties"].pop("metrics"), "removed"),
        (
            "version const",
            lambda schema: schema["properties"]["version"].update(const="0.3.0"),
            "version",
        ),
    ],
)
def test_a_breaking_schema_change_is_potentially_incompatible(
    tmp_path: Path,
    upstream: Upstream,
    name: str,
    edit: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    upstream.commit(name, {"core-spec/ossie-schema.json": schema_with(edit)})
    (change,) = report_of(tmp_path, upstream).changes
    assert change.kind == "potentially incompatible"
    assert reason in change.reason


def test_an_expression_language_edit_is_a_critical_review(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.commit("spec: nulls", {"core-spec/expression_language.md": "# Expressions\n\nNo.\n"})
    report = report_of(tmp_path, upstream)
    assert change_class(report, "core-spec/expression_language.md") == "critical review"
    (annotation,) = report.annotations()
    assert annotation.startswith("::warning ")
    assert "core-spec/expression_language.md" in annotation


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ("A metric aggregates a field.", "A metric sums a field.", "critical review"),
        ("Describe models.", "Describe semantic models.", "potentially incompatible"),
        ("- 0.2.0.dev0", "- 0.2.0.dev1", "informational"),
        ("# Not a heading", "# Still not a heading", "potentially incompatible"),
    ],
    ids=["metrics-section", "goals-section", "version-history", "code-block-under-goals"],
)
def test_a_spec_edit_is_classified_by_its_section(
    tmp_path: Path, upstream: Upstream, old: str, new: str, kind: str
) -> None:
    upstream.commit("spec", {"core-spec/spec.md": SPEC.replace(old, new)})
    assert change_class(report_of(tmp_path, upstream), "core-spec/spec.md") == kind


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        (FILES["extensions/dialects.md"] + "| MAQL |\n", "compatible"),
        (FILES["extensions/dialects.md"].replace("| DAX |\n", ""), "potentially incompatible"),
    ],
    ids=["addition", "removal"],
)
def test_a_catalog_addition_is_compatible_and_a_removal_is_not(
    tmp_path: Path, upstream: Upstream, content: str, kind: str
) -> None:
    upstream.commit("catalog", {"extensions/dialects.md": content})
    assert change_class(report_of(tmp_path, upstream), "extensions/dialects.md") == kind


def test_an_unreadable_schema_is_potentially_incompatible() -> None:
    before = FILES["core-spec/ossie-schema.json"].encode()
    found = watch.classify("core-spec/ossie-schema.json", before, b"{not json")
    assert found.kind == "potentially incompatible"


# --- Vendored files and commits ----------------------------------------------------------------


def test_a_vendored_file_changed_upstream_is_reported_with_both_hashes(
    tmp_path: Path, upstream: Upstream
) -> None:
    new = SPEC.replace("Describe models.", "Describe semantic models.")
    upstream.commit("spec", {"core-spec/spec.md": new})
    report = report_of(tmp_path, upstream)
    (vendored,) = report.vendored
    assert vendored.file == "spec.md"
    assert vendored.upstream == "core-spec/spec.md"
    assert vendored.pinned_sha256 == hashlib.sha256(SPEC.encode()).hexdigest()
    assert vendored.upstream_sha256 == hashlib.sha256(new.encode()).hexdigest()


def test_a_vendored_file_removed_upstream_has_no_new_hash(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.commit("drop", {"core-spec/expression_language.md": None})
    (vendored,) = report_of(tmp_path, upstream).vendored
    assert vendored.upstream_sha256 is None


def test_nothing_changed_upstream_reports_nothing(tmp_path: Path, upstream: Upstream) -> None:
    report = report_of(tmp_path, upstream)
    assert (report.vendored, report.changes, report.commits) == ([], [], [])
    assert report.upstream == upstream.pinned


def test_a_commit_touching_only_unwatched_paths_is_not_reported(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.commit("docs: root", {"README.md": "# Upstream 2\n", "examples/model.yaml": "x: 1\n"})
    watched = upstream.commit("docs: core", {"core-spec/README.md": "# Core 2\n"})
    report = report_of(tmp_path, upstream)
    assert [commit.sha for commit in report.commits] == [watched]
    assert report.commits[0].paths == ["core-spec/README.md"]
    assert report.commits[0].kind == "informational"


def test_a_commit_takes_the_highest_class_of_its_files(tmp_path: Path, upstream: Upstream) -> None:
    upstream.commit(
        "mixed",
        {"core-spec/README.md": "# Core 2\n", "core-spec/expression_language.md": "# E\n"},
    )
    (commit,) = report_of(tmp_path, upstream).commits
    assert commit.kind == "critical review"


# --- Proposals ---------------------------------------------------------------------------------

ANSWERS: dict[int, dict[str, Any]] = {
    587: {"number": 587, "state": "closed", "pull_request": {"merged_at": "2026-11-05T04:28:50Z"}},
    522: {"number": 522, "state": "open", "pull_request": {"merged_at": None}},
    246: {"number": 246, "state": "closed", "pull_request": {"merged_at": None}},
    300: {"number": 300, "state": "closed", "state_reason": "completed"},
}


def answer(number: int) -> dict[str, Any]:
    return ANSWERS[number]


@pytest.mark.parametrize(
    ("recorded", "lines"),
    [
        ({587: "open"}, ["#587: open -> merged"]),
        ({522: "open", 246: "closed", 300: "closed"}, []),
        ({300: "open"}, ["#300: open -> closed"]),
        ({246: "unrecorded"}, ["#246: unrecorded -> closed"]),
    ],
    ids=["merged", "unchanged", "issue-closed", "unrecorded"],
)
def test_a_proposal_state_change_is_one_line(
    tmp_path: Path, upstream: Upstream, recorded: dict[int, str], lines: list[str]
) -> None:
    watch_list, pin = write_inputs(tmp_path, upstream, recorded)
    report = watch.run(watch.load_watch_list(watch_list), watch.load_pin(pin), answer)
    assert [proposal.line() for proposal in report.proposals] == lines


def test_an_unknown_proposal_state_fails(tmp_path: Path, upstream: Upstream) -> None:
    watch_list, pin = write_inputs(tmp_path, upstream, {522: "open"})
    with pytest.raises(watch.WatchError, match="522"):
        watch.run(
            watch.load_watch_list(watch_list),
            watch.load_pin(pin),
            lambda number: {"number": number, "state": "locked"},
        )


# --- Outputs and exit status -------------------------------------------------------------------


def test_findings_succeed_with_a_summary_a_json_artifact_and_warnings(
    tmp_path: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    upstream.commit("spec: nulls", {"core-spec/expression_language.md": "# Expressions\n\nNo.\n"})
    watch_list, pin = write_inputs(tmp_path, upstream, {587: "open"})
    summary, artifact = tmp_path / "summary.md", tmp_path / "out" / "report.json"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    arguments = ["--watch", str(watch_list), "--pin", str(pin), "--json", str(artifact)]
    assert watch.main(arguments, fetch=answer) == 0
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert set(data) == {
        "upstream",
        "pinned",
        "classes",
        "vendored",
        "changes",
        "commits",
        "proposals",
    }
    assert data["classes"] == {"critical review": ["core-spec/expression_language.md"]}
    assert data["vendored"][0]["file"] == "expression_language.md"
    assert data["proposals"] == [{"number": 587, "recorded": "open", "state": "merged"}]
    text = summary.read_text(encoding="utf-8")
    assert "critical review" in text
    assert "#587: open -> merged" in text
    assert "::warning " in capsys.readouterr().out


@pytest.mark.parametrize(
    "content",
    [
        "repository = 'x'\n",
        "repository = 'x'\napi = 'y'\nbranch = 'main'\npaths = []\n[proposals]\n",
        "repository = 'x'\napi = 'y'\nbranch = 'main'\npaths = ['a/']\n[proposals]\n1 = 'shut'\n",
        "repository = 'x'\napi = 'y'\nbranch = 'main'\npaths = ['a/']\n[proposals]\nb = 'open'\n",
        "repository = 'x'\napi = 'y'\nbranch = 'main'\npaths = [1]\n[proposals]\n",
        "repository = [",
    ],
    ids=["missing-keys", "no-paths", "bad-state", "bad-number", "bad-path", "not-toml"],
)
def test_a_malformed_watch_list_fails(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_list = tmp_path / "watch.toml"
    watch_list.write_text(content, encoding="utf-8")
    pin = REPOSITORY_ROOT / "vendor" / "apache-ossie" / "PIN.toml"
    assert watch.main(["--watch", str(watch_list), "--pin", str(pin)], fetch=no_proposal) == 1
    assert capsys.readouterr().out.startswith("::error::")


def test_an_unreachable_upstream_fails(tmp_path: Path, upstream: Upstream) -> None:
    watch_list, pin = write_inputs(tmp_path, upstream)
    watch_list.write_text(
        watch_list.read_text(encoding="utf-8").replace(
            json.dumps(str(upstream.root)), json.dumps(str(tmp_path / "missing"))
        ),
        encoding="utf-8",
    )
    assert watch.main(["--watch", str(watch_list), "--pin", str(pin)], fetch=no_proposal) == 1


def test_a_pinned_commit_missing_upstream_fails(tmp_path: Path, upstream: Upstream) -> None:
    watch_list, pin = write_inputs(tmp_path, upstream)
    pin.write_text(
        pin.read_text(encoding="utf-8").replace(upstream.pinned, "0" * 40), encoding="utf-8"
    )
    assert watch.main(["--watch", str(watch_list), "--pin", str(pin)], fetch=no_proposal) == 1


def test_an_unreachable_api_fails(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: urllib.request.Request, timeout: float) -> None:
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    watch_list, pin = write_inputs(tmp_path, upstream, {587: "open"})
    assert watch.main(["--watch", str(watch_list), "--pin", str(pin)]) == 1


def test_the_repository_watch_list_and_pin_load() -> None:
    folder = REPOSITORY_ROOT / "vendor" / "apache-ossie"
    watch_list = watch.load_watch_list(folder / "watch.toml")
    pin = watch.load_pin(folder / "PIN.toml")
    assert watch_list.repository == "https://github.com/apache/ossie"
    assert set(watch_list.proposals) == {246, 287, 522, 529, 568, 573, 587, 633, 647}
    assert "core-spec/" in watch_list.paths
    assert set(pin.files) == set(pin.sha256)


# --- Never -------------------------------------------------------------------------------------


def tree_digest(*roots: Path) -> str:
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(root.rglob("*") if root.is_dir() else [root]):
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(str(path).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def test_it_never_changes_the_registry_a_vendored_file_a_contract_or_a_version(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.commit("spec", {"core-spec/ossie-schema.json": "{}\n"})
    watch_list, pin = write_inputs(tmp_path, upstream, {587: "open"})
    guarded = (
        REPOSITORY_ROOT / "vendor",
        REPOSITORY_ROOT / "packages/semantic/src/orvex/semantic/ossie",
        REPOSITORY_ROOT / "contracts",
        REPOSITORY_ROOT / "versions.toml",
        watch_list,
        pin,
    )
    before = tree_digest(*guarded)
    arguments = ["--watch", str(watch_list), "--pin", str(pin), "--json", str(tmp_path / "r.json")]
    assert watch.main(arguments, fetch=answer) == 0
    assert tree_digest(*guarded) == before


def test_it_only_reads_the_api(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[urllib.request.Request] = []

    class Answer:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *exception: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def record(request: urllib.request.Request, timeout: float) -> Answer:
        requests.append(request)
        return Answer(json.dumps(ANSWERS[587]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", record)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    watch_list, pin = write_inputs(tmp_path, upstream, {587: "open"})
    assert watch.main(["--watch", str(watch_list), "--pin", str(pin)]) == 0
    (request,) = requests
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == "https://api.github.com/repos/apache/ossie/issues/587"


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.run

    def record(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:  # noqa: ANN401
        calls.append(list(arguments))
        done: subprocess.CompletedProcess[bytes] = real(arguments, **options)
        return done

    monkeypatch.setattr(subprocess, "run", record)
    return calls


def test_it_never_pushes(tmp_path: Path, upstream: Upstream, git_calls: list[list[str]]) -> None:
    upstream.commit("spec: nulls", {"core-spec/expression_language.md": "# E\n"})
    report_of(tmp_path, upstream)
    subcommands = {next(arg for arg in call[1:] if not arg.startswith("-")) for call in git_calls}
    assert git_calls
    assert all(call[0] == "git" for call in git_calls)
    assert subcommands <= {"clone", "rev-parse", "cat-file", "diff", "log", "ls-tree"}
    source = (REPOSITORY_ROOT / "tools" / "ossie" / "watch.py").read_text(encoding="utf-8")
    assert not re.search(r"\bpush\b|\bgh\b|\"(POST|PUT|PATCH|DELETE)\"", source)


# --- Workflow ----------------------------------------------------------------------------------

FORBIDDEN = re.compile(
    r"\bgh\s+(issue|pr|api)\b|\bgit\s+push\b|\bcurl\b|"
    r"(-X|--method|--request)\s*['\"]?(POST|PUT|PATCH|DELETE)",
    re.IGNORECASE,
)


def workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def test_the_workflow_runs_only_on_a_schedule_or_by_hand() -> None:
    document = workflow()
    events: dict[str, Any] = document.get("on", document.get(True)) or {}
    assert set(events) == {"schedule", "workflow_dispatch"}
    assert events["schedule"]


def test_the_workflow_only_reads_contents() -> None:
    document = workflow()
    assert document["permissions"] == {"contents": "read"}
    for job in document["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
        assert "secrets." not in json.dumps(job)


def test_no_workflow_step_writes() -> None:
    steps = [step for job in workflow()["jobs"].values() for step in job["steps"]]
    actions = {step["uses"].split("@")[0] for step in steps if "uses" in step}
    assert actions <= {"actions/checkout", "actions/setup-python", "actions/upload-artifact"}
    runs = [step["run"] for step in steps if "run" in step]
    assert any("python -m tools.ossie.watch" in run for run in runs)
    for run in runs:
        assert not FORBIDDEN.search(run), run
    for step in steps:
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] is False


@pytest.mark.parametrize(
    "run",
    ["gh issue create", "gh pr comment 1", "git push origin x", "curl -X POST https://x"],
)
def test_the_write_check_catches_a_write(run: str) -> None:
    assert FORBIDDEN.search(run)


def test_the_job_is_not_a_required_check() -> None:
    names = set(workflow()["jobs"]) | {job.get("name", "") for job in workflow()["jobs"].values()}
    assert not names & listed_required()
    listed = (REPOSITORY_ROOT / "tools" / "ci" / "checks.toml").read_text(encoding="utf-8")
    assert WORKFLOW.name not in listed
