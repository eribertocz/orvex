"""Semantic corpus runner (P0-11).

    python -m conformance.runners.semantic_corpus [--through library|http|both|existing-ducklake]
        [--switch-on] [--json <file>]
    (also: --through clickhouse)

Loads every imported case, checks that it is complete and unchanged, validates its Ossie model
against the pinned specification, and reports it. Until the engine executes queries (P2-12) every
case is `not-run` with the reason ENGINE_NOT_AVAILABLE. Since P2-27a (#363) every case is also
asked through the HTTP API, and the run is the required `semantic-corpus` check: exit status 0
only when every case loads, none fails, and both ways give the same outcome. Since P2-30 (#399),
`--through existing-ducklake` also asks every case on an existing DuckLake with a PostgreSQL
catalog (`corpus_existing_ducklake`) and compares it with the library: the corpus parity.
Since P2-31 (#225), `--through clickhouse` asks every case on ClickHouse (`corpus_clickhouse`)
and compares it with the library in the same way. Since #422, a case run but left `not-run` for
any reason other than an engine's declared refusal is a finding: the runner never skips silently.
Since #427, each way's reports must be exactly the manifest's cases, each once and each passed:
a case missing, repeated, unknown, refused or not run fails the check by itself, unless
`conformance/semantic-corpus/expected-fail.toml` (governed: the Owner merges it) lists it with
an open issue and a due phase not yet passed; such a case that passes fails too, so its entry
is removed. A known gap excuses only its own case's run, never a changed source or model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from conformance.runners import bi_platform_adapter as adapter

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path("conformance/semantic-corpus/bi-platform")
OSSIE = Path("vendor/apache-ossie")
# Outside the imported corpus, whose files are each pinned to upstream (governed: Owner merges).
GAPS = Path("conformance/semantic-corpus/expected-fail.toml")
# What a known gap may excuse: its own case failing, not running or differing between ways.
# A changed source, a manifest mismatch or an invalid model is never a known gap.
EXCUSABLE = frozenset(
    {"CORPUS_CASE_FAILED", "CORPUS_CASE_NOT_RUN", "CORPUS_CASE_NOT_PASSED", "CORPUS_WAYS_DIFFER"}
)


@dataclass(frozen=True, slots=True)
class CaseReport:
    case: str
    title: str
    expected: str
    error_code: str | None
    model: str
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    case: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.case}: {self.message}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_sources(corpus: Path) -> list[Finding]:
    """Every imported file matches SOURCE.toml, and no file is added or missing."""
    recorded: dict[str, str] = tomllib.loads((corpus / "SOURCE.toml").read_text("utf-8"))["sha256"]
    present = {
        path.relative_to(corpus).as_posix(): path
        for path in corpus.rglob("*")
        if path.is_file() and path != corpus / "SOURCE.toml"
    }
    findings = []
    for name in sorted(set(recorded) | set(present)):
        if name not in present:
            findings.append(Finding("CORPUS_SOURCE_CHANGED", name, "missing"))
        elif name not in recorded:
            findings.append(Finding("CORPUS_SOURCE_CHANGED", name, "not in SOURCE.toml"))
        elif sha256(present[name]) != recorded[name]:
            findings.append(Finding("CORPUS_SOURCE_CHANGED", name, "SHA-256 differs"))
    return findings


def check_vendored(ossie: Path) -> list[Finding]:
    """Every vendored Ossie file matches PIN.toml, and no file is added or missing."""
    recorded: dict[str, str] = tomllib.loads((ossie / "PIN.toml").read_text("utf-8"))["sha256"]
    present = {path.name: path for path in ossie.iterdir() if path.name != "PIN.toml"}
    findings = []
    for name in sorted(set(recorded) | set(present)):
        where = f"{OSSIE.as_posix()}/{name}"
        if name not in present:
            findings.append(Finding("CORPUS_SOURCE_CHANGED", where, "missing"))
        elif name not in recorded:
            findings.append(Finding("CORPUS_SOURCE_CHANGED", where, "not in PIN.toml"))
        elif not present[name].is_file() or sha256(present[name]) != recorded[name]:
            findings.append(Finding("CORPUS_SOURCE_CHANGED", where, "SHA-256 differs"))
    return findings


def ossie_validator(ossie: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads((ossie / "ossie-schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def check_manifest(corpus: Path, manifest: dict[str, object]) -> list[Finding]:
    """The manifest lists exactly the case folders, and its case count agrees."""
    cases = manifest.get("cases")
    listed = (
        [entry.get("id") for entry in cases if isinstance(entry, dict)]
        if isinstance(cases, list)
        else []
    )
    folders = sorted(path.name for path in (corpus / "corpus" / "cases").iterdir() if path.is_dir())
    findings = []
    for case in sorted(set(folders) - set(listed)):
        findings.append(
            Finding("CORPUS_MANIFEST_MISMATCH", case, "case folder not in manifest.json")
        )
    for case in sorted({str(case) for case in listed} - set(folders)):
        findings.append(
            Finding("CORPUS_MANIFEST_MISMATCH", case, "listed in manifest.json, no folder")
        )
    if manifest.get("case_count") != len(listed):
        detail = f"case_count is {manifest.get('case_count')}, {len(listed)} cases are listed"
        findings.append(Finding("CORPUS_MANIFEST_MISMATCH", "manifest.json", detail))
    return findings


def load_case(
    corpus: Path,
    entry: dict[str, str],
    validator: jsonschema.Draft202012Validator,
    execute: bool = True,
    through: str = "library",
) -> tuple[CaseReport | None, list[Finding]]:
    case = entry["id"]
    folder = corpus / "corpus" / "cases" / case
    findings: list[Finding] = []
    result = folder / "expected" / "result.csv"
    error = folder / "expected" / "error.json"
    inputs = sorted((folder / "input").glob("*.csv"))
    if not (folder / "README.md").is_file() or not inputs or result.exists() == error.exists():
        detail = "needs a README, an input CSV and exactly one expected result or error"
        return None, [Finding("CORPUS_CASE_INCOMPLETE", case, detail)]
    expected = "result" if result.exists() else "error"
    if adapter.OUTCOMES.get(entry["outcome"]) != expected:
        detail = f"manifest says {entry['outcome']}, the case has an expected {expected}"
        findings.append(Finding("CORPUS_MANIFEST_MISMATCH", case, detail))
    for path in [*inputs, result] if result.exists() else inputs:
        try:
            adapter.read_csv(path)
        except (ValueError, csv.Error) as problem:
            findings.append(Finding("CORPUS_CASE_INCOMPLETE", case, f"unreadable CSV: {problem}"))
    code = None
    if error.exists():
        try:
            upstream = json.loads(error.read_text("utf-8"))["code"]
            if not isinstance(upstream, str):
                raise TypeError("`code` is not a string")
        except (ValueError, KeyError, TypeError) as problem:
            detail = f"unreadable expected/error.json: {problem}"
            return None, [*findings, Finding("CORPUS_CASE_INCOMPLETE", case, detail)]
        code = adapter.product_error_code(upstream)
        if code is None:
            findings.append(Finding("CORPUS_ERROR_CODE_UNMAPPED", case, upstream))
    model_path = corpus / "models" / f"{case}.yaml"
    model = "absent"
    if model_path.exists():
        try:
            document = adapter.load_yaml(model_path)
            adapter.load_yaml(corpus / "models" / f"{case}.catalog.yaml")
        except (OSError, yaml.YAMLError) as problem:
            return None, [*findings, Finding("CORPUS_MODEL_INVALID", case, str(problem))]
        problems = sorted(error.message for error in validator.iter_errors(document))
        if problems:
            findings.append(Finding("CORPUS_MODEL_INVALID", case, problems[0]))
        else:
            model = "valid"
    status, reason = "not-run", "MODEL_INVALID" if model != "valid" else "NOT_EXECUTED"
    if model == "valid" and execute:
        if through == "http":
            from conformance.runners.corpus_http import run_case
        elif through == "existing-ducklake":
            from conformance.runners.corpus_existing_ducklake import run_case
        elif through == "clickhouse":
            from conformance.runners.corpus_clickhouse import run_case
        else:
            from conformance.runners.corpus_execution import run_case

        outcome = run_case(case, folder, model_path, code)
        status, reason = outcome.status, outcome.reason
        if status == "not-run" and not outcome.refused:  # the runner's skip, never a pass (#422)
            findings.append(Finding("CORPUS_CASE_NOT_RUN", case, f"{through}: {reason}"))
    report = CaseReport(
        case=case,
        title=entry["title"],
        expected=expected,
        error_code=code,
        model=model,
        status=status,
        reason=reason,
    )
    return report, findings


def run(
    root: Path = ROOT, execute: bool = True, through: str = "library"
) -> tuple[list[CaseReport], list[Finding]]:
    """Every case checked, and run for real unless `execute` is false (P2-12), through the
    library, the HTTP API (`through`, P2-27a), on an existing DuckLake (P2-30) or on ClickHouse
    (P2-31)."""
    corpus = root / CORPUS
    findings = check_sources(corpus) + check_vendored(root / OSSIE)
    try:
        validator = ossie_validator(root / OSSIE)
    except (OSError, ValueError, jsonschema.SchemaError) as problem:
        # Without the pinned schema no model can be checked; the source finding names the file.
        return [], [*findings, Finding("CORPUS_SOURCE_CHANGED", OSSIE.as_posix(), str(problem))]
    manifest = json.loads((corpus / "corpus" / "manifest.json").read_text("utf-8"))
    findings += check_manifest(corpus, manifest)
    reports = []
    for entry in manifest["cases"]:
        if not (corpus / "corpus" / "cases" / entry["id"]).is_dir():
            continue
        report, case_findings = load_case(corpus, entry, validator, execute, through)
        findings.extend(case_findings)
        if report is not None:
            reports.append(report)
    return reports, findings


def expected_failures(path: Path, minor: int) -> tuple[dict[str, str], list[Finding]]:
    """The known gaps of `expected-fail.toml` (`CASE-NNN = {issue = n, due = "P3"}`), and a
    finding for each entry without an issue or a due phase, or whose due phase has passed (the
    current phase is P<minor> of the product version: `0.3.x` ships P2 and works on P3)."""
    if not path.is_file():
        return {}, []
    listed = tomllib.loads(path.read_text("utf-8"))
    gaps: dict[str, str] = {}
    findings = []
    for case, entry in listed.items():
        issue, due = entry.get("issue"), str(entry.get("due", ""))
        if not isinstance(issue, int) or not due.startswith("P") or not due[1:].isdigit():
            findings.append(Finding("CORPUS_EXPECTED_FAIL_INVALID", case, "needs issue and due"))
        elif int(due[1:]) < minor:
            findings.append(Finding("CORPUS_EXPECTED_FAIL_OVERDUE", case, f"due {due}, #{issue}"))
        else:
            gaps[case] = f"#{issue}"
    return gaps, findings


def excuse(findings: list[Finding], gaps: dict[str, str]) -> list[Finding]:
    """`findings` without those a known gap of `gaps` excuses (`EXCUSABLE`, its own case)."""
    return [f for f in findings if not (f.code in EXCUSABLE and f.case in gaps)]


def exact_cases(
    manifest: dict[str, Any], reports: list[CaseReport], way: str, gaps: dict[str, str]
) -> list[Finding]:
    """A finding unless `reports` are exactly the manifest's cases, each once, each passed
    (a known gap of `gaps` fails instead, and a gap that passes is a finding too)."""
    expected = [str(entry["id"]) for entry in manifest["cases"]]
    seen = [report.case for report in reports]
    findings = [
        *(Finding("CORPUS_CASE_MISSING", c, way) for c in expected if c not in seen),
        *(Finding("CORPUS_CASE_UNKNOWN", c, way) for c in sorted(set(seen) - set(expected))),
        *(
            Finding("CORPUS_CASE_DUPLICATE", c, way)
            for c in sorted({c for c in seen if seen.count(c) > 1})
        ),
    ]
    for report in reports:
        if report.case in gaps:
            if report.status == "pass":
                findings.append(Finding("CORPUS_EXPECTED_FAIL_PASSED", report.case, way))
        elif report.status != "pass":
            message = f"{way}: {report.status} ({report.reason})"
            findings.append(Finding("CORPUS_CASE_NOT_PASSED", report.case, message))
    return findings


def compare(
    library: list[CaseReport], other_way: list[CaseReport], name: str = "http"
) -> list[Finding]:
    """A finding for every case that fails either way, or whose outcome differs between the
    library and the other way `name` (the HTTP API, an existing DuckLake or ClickHouse)."""
    findings = []
    by_case = {report.case: report for report in other_way}
    for report in library:
        other = by_case.get(report.case)
        for way, found in (("library", report), (name, other)):
            if found is not None and found.status == "fail":
                findings.append(Finding("CORPUS_CASE_FAILED", found.case, f"{way}: {found.reason}"))
        if other is None or (other.status, other.reason) != (report.status, report.reason):
            seen = "missing" if other is None else f"{other.status} ({other.reason})"
            message = f"library {report.status} ({report.reason}), {name} {seen}"
            findings.append(Finding("CORPUS_WAYS_DIFFER", report.case, message))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--json", type=Path, help="write the deterministic report to this file")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--through",
        choices=("library", "http", "both", "existing-ducklake", "clickhouse"),
        default="library",
        help="ask through the library, the HTTP API, or both and compare them; or through the "
        "library and on an existing DuckLake or on ClickHouse, and compare them",
    )
    parser.add_argument(
        "--switch-on",
        action="store_true",
        help="turn the Direct Query development switches on for this run (existing DuckLake "
        "and ClickHouse; their variables named from identity.toml)",
    )
    arguments = parser.parse_args()
    if arguments.switch_on:
        from orvex.server import development_switch
        from orvex.server.identity import load_identity

        identity = load_identity()
        for suffix in (development_switch.SUFFIX, development_switch.CLICKHOUSE_SUFFIX):
            os.environ[development_switch.variable(identity, suffix)] = "1"
    ways = {
        "both": ("library", "http"),
        "existing-ducklake": ("library", "existing-ducklake"),
        "clickhouse": ("library", "clickhouse"),
    }.get(arguments.through, (arguments.through,))
    runs = {way: run(arguments.root, through=way) for way in ways}
    reports, findings = runs[ways[0]]
    corpus = arguments.root / CORPUS
    manifest = json.loads((corpus / "corpus" / "manifest.json").read_text("utf-8"))
    versions = arguments.root / "versions.toml"
    if not versions.is_file():  # a test's copy of the corpus alone: the repository's phase
        versions = ROOT / "versions.toml"
    product = tomllib.loads(versions.read_text("utf-8"))["product"]
    gaps, gap_findings = expected_failures(arguments.root / GAPS, int(str(product).split(".")[1]))
    findings = [
        *findings,
        *gap_findings,
        *(f for way in ways for f in exact_cases(manifest, runs[way][0], way, gaps)),
    ]
    if len(ways) == 2:
        not_run = [f for f in runs[ways[1]][1] if f.code == "CORPUS_CASE_NOT_RUN"]
        findings = [*findings, *not_run, *compare(reports, runs[ways[1]][0], ways[1])]
    else:
        findings = [
            *findings,
            *(
                Finding("CORPUS_CASE_FAILED", r.case, f"{ways[0]}: {r.reason}")
                for r in reports
                if r.status == "fail"
            ),
        ]
    findings = excuse(findings, gaps)
    for way in ways:
        for report in runs[way][0]:
            print(
                f"{report.case} {report.status} ({report.reason}) "
                f"expected={report.expected} model={report.model} through={way}"
            )
    for finding in findings:
        print(finding)
    print(f"semantic-corpus: {len(reports)} case(s), {len(findings)} finding(s)")
    if arguments.json:
        document = {
            "cases": [asdict(report) for report in reports],
            "findings": [asdict(f) for f in findings],
        }
        arguments.json.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
