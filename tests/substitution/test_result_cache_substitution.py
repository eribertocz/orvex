"""The real substitution test of #486 G1: the server, assembled by configuration only with the
independent alternative result cache (`conformance/alternatives/result_cache_ipc`), passes the
same end-to-end result-cache suite as with the semantic module's cache, with no consumer file
changed and no case or operation omitted; and each way to cheat that comparison is caught.

The alternative is built and installed from its own source into a folder of this test, never
into the environment, and only the run that uses it sees that folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from orvex.contracts.semantic.interface import RESULT_CACHE

from tests.support.fixtures import frozen
from tools.repository import REPOSITORY_ROOT, identity

ALTERNATIVE = REPOSITORY_ROOT / "conformance" / "alternatives" / "result_cache_ipc"
ALTERNATIVE_MODULE = "alternative_result_cache_ipc"
SUITE = "packages/server/tests/test_server_result_cache_suite.py"
# The consumers that may not change between the two runs: the server and the runners.
CONSUMERS = ("packages/server/src", "conformance/runners")
CONSUMER_SUFFIXES = {".py", ".toml", ".json", ".yaml", ".sql", ".txt"}


def consumer_digest(root: Path = REPOSITORY_ROOT) -> dict[str, str]:
    """The SHA-256 of every consumer file, by its path."""
    found: dict[str, str] = {}
    for start in CONSUMERS:
        for path in sorted((root / start).rglob("*")):
            if (
                path.is_file()
                and path.suffix in CONSUMER_SUFFIXES
                and "__pycache__" not in path.parts
            ):
                found[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return found


def choice_variable() -> str:
    slug = str(identity(REPOSITORY_ROOT)["slug"])
    return f"{slug.upper().replace('-', '_')}_IMPLEMENTATION_RESULT_CACHE"


def install_alternative(target: Path) -> None:
    """Build and install the alternative from a copy of its source, so the checkout is untouched."""
    source = target.with_name("alternative-source")
    shutil.copytree(
        ALTERNATIVE, source, ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__")
    )
    command = [
        sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
        "--no-build-isolation", "--target", str(target), str(source),
    ]  # fmt: skip
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def run_suite(record: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    """One run of the end-to-end suite, in its own process, and its record."""
    command = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-p", "tests.substitution.recording", SUITE,
    ]  # fmt: skip
    done = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env={**environment, "SUBSTITUTION_RECORD": str(record)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert record.is_file(), done.stdout[-3000:] + done.stderr[-3000:]
    found: dict[str, Any] = json.loads(record.read_text(encoding="utf-8"))
    found["exit"] = done.returncode
    return found


def run_problems(original: Mapping[str, Any], alternative: Mapping[str, Any]) -> list[str]:
    """Every way the run with the alternative falls short of the run with the original."""
    problems: list[str] = []
    for name, run in (("original", original), ("alternative", alternative)):
        failed = [case for case, outcome in run["cases"] if outcome != "passed"]
        if failed or run["exit"] != 0 or not run["cases"]:
            problems.append(f"SUBSTITUTION_CASE_FAILED: the {name} run failed {failed}")
    if [c for c, _ in original["cases"]] != [c for c, _ in alternative["cases"]]:
        problems.append(
            f"SUBSTITUTION_CASES_DIFFER: {len(original['cases'])} cases with the original, "
            f"{len(alternative['cases'])} with the alternative"
        )
    if original["operations"] != alternative["operations"]:
        problems.append("SUBSTITUTION_OPERATIONS_DIFFER: the runs called different operations")
    if not {o for _, o in original["operations"]} >= {
        o.name for o in RESULT_CACHE.operations if o.name in {"get", "put"}
    }:
        problems.append("SUBSTITUTION_OPERATIONS_DIFFER: the suite never reads or writes the cache")
    chosen = alternative["implementations"]
    if len(chosen) != 1 or chosen[0].split(".")[1] != ALTERNATIVE_MODULE:
        problems.append(f"SUBSTITUTION_NOT_SUBSTITUTED: the alternative run used {chosen}")
    if original["implementations"] == chosen:
        problems.append("SUBSTITUTION_NOT_SUBSTITUTED: both runs used one implementation")
    return problems


def digest_problems(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    return [f"SUBSTITUTION_CONSUMER_CHANGED: {path}" for path in changed]


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    work = tmp_path_factory.mktemp("substitution")
    before = consumer_digest()
    base = {k: v for k, v in os.environ.items() if k != choice_variable()}
    original = run_suite(work / "original.json", base)
    middle = consumer_digest()
    install_alternative(work / "alternative")
    path = os.pathsep.join(filter(None, [str(work / "alternative"), base.get("PYTHONPATH")]))
    alternative = run_suite(
        work / "alternative.json",
        {**base, "PYTHONPATH": path, choice_variable(): ALTERNATIVE_MODULE},
    )
    found = {
        "original": original,
        "alternative": alternative,
        "digests": (before, middle, consumer_digest()),
    }
    for _ in frozen(work):  # the runs' files stay as the runs left them
        yield found


def test_the_server_passes_the_same_suite_with_the_alternative(runs: dict[str, Any]) -> None:
    assert run_problems(runs["original"], runs["alternative"]) == []
    before, middle, after = runs["digests"]
    assert digest_problems(before, middle) == digest_problems(middle, after) == []
    assert len(runs["original"]["cases"]) == len(runs["alternative"]["cases"]) == 7


def test_a_run_with_one_case_fewer_fails(runs: dict[str, Any]) -> None:
    fewer = {**runs["alternative"], "cases": runs["alternative"]["cases"][:-1]}
    assert any(
        p.startswith("SUBSTITUTION_CASES_DIFFER") for p in run_problems(runs["original"], fewer)
    )


def test_a_run_that_omits_an_operation_fails(runs: dict[str, Any]) -> None:
    operations = [pair for pair in runs["alternative"]["operations"] if pair[1] != "put"]
    omitted = {**runs["alternative"], "operations": operations}
    found = run_problems(runs["original"], omitted)
    assert any(p.startswith("SUBSTITUTION_OPERATIONS_DIFFER") for p in found)


def test_a_run_that_did_not_use_the_alternative_fails(runs: dict[str, Any]) -> None:
    same = {**runs["alternative"], "implementations": runs["original"]["implementations"]}
    found = run_problems(runs["original"], same)
    assert any(p.startswith("SUBSTITUTION_NOT_SUBSTITUTED") for p in found)


def test_a_consumer_file_changed_to_make_the_alternative_work_fails(tmp_path: Path) -> None:
    for start in CONSUMERS:
        (tmp_path / start).mkdir(parents=True)
    consumer = tmp_path / CONSUMERS[0] / "semantic_http.py"
    consumer.write_text("cache = None\n", encoding="utf-8")
    before = consumer_digest(tmp_path)
    consumer.write_text("cache = Alternative()\n", encoding="utf-8")
    assert digest_problems(before, consumer_digest(tmp_path)) == [
        f"SUBSTITUTION_CONSUMER_CHANGED: {CONSUMERS[0]}/semantic_http.py"
    ]
