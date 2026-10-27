"""Semantic corpus runner (P0-11).

    python -m conformance.runners.semantic_corpus [--json <file>]

Loads every imported case, checks that it is complete and unchanged, validates its Ossie model
against the pinned specification, and reports it. Until the engine executes queries (P2-12) every
case is `not-run` with the reason ENGINE_NOT_AVAILABLE. Exit status 0 when every case loads.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import jsonschema
import yaml

from conformance.runners import bi_platform_adapter as adapter

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path("conformance/semantic-corpus/bi-platform")
OSSIE = Path("vendor/apache-ossie")


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
    corpus: Path, entry: dict[str, str], validator: jsonschema.Draft202012Validator
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
    report = CaseReport(
        case=case,
        title=entry["title"],
        expected=expected,
        error_code=code,
        model=model,
        status="not-run",
        reason="ENGINE_NOT_AVAILABLE",
    )
    return report, findings


def run(root: Path = ROOT) -> tuple[list[CaseReport], list[Finding]]:
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
        report, case_findings = load_case(corpus, entry, validator)
        findings.extend(case_findings)
        if report is not None:
            reports.append(report)
    return reports, findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--json", type=Path, help="write the deterministic report to this file")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    reports, findings = run(arguments.root)
    for report in reports:
        print(
            f"{report.case} {report.status} ({report.reason}) "
            f"expected={report.expected} model={report.model}"
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
