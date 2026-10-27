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
        if path.is_file() and path.name != "SOURCE.toml"
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


def ossie_validator(ossie: Path) -> jsonschema.Draft202012Validator:
    pin = tomllib.loads((ossie / "PIN.toml").read_text("utf-8"))
    for name, digest in pin["sha256"].items():
        if sha256(ossie / name) != digest:
            raise ValueError(f"{ossie / name} differs from PIN.toml")
    schema = json.loads((ossie / "ossie-schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


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
        upstream = json.loads(error.read_text("utf-8"))["code"]
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
    findings = check_sources(corpus)
    validator = ossie_validator(root / OSSIE)
    manifest = json.loads((corpus / "corpus" / "manifest.json").read_text("utf-8"))
    reports = []
    for entry in manifest["cases"]:
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
