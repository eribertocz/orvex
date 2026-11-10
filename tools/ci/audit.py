"""Known vulnerabilities in the locked dependencies, against their recorded exceptions (#429).

    pip-audit -f json --progress-spinner off > pip-audit.json
    python -m tools.ci.audit pip pip-audit.json
    npm audit --json > npm-audit.json   # in web/
    python -m tools.ci.audit npm npm-audit.json

A known vulnerability fails the run (for npm, those of severity `high` or `critical`), unless
`tools/ci/audit-exceptions.toml` records it with its ecosystem (`pip` or `npm`), package,
advisory ID, the reason it does not apply or has no fix, and a review date not yet passed. An
exception that matches nothing, or whose review date has passed, fails too, so the list never
outlives its reasons. The audit output is
read as data; a report that cannot be read fails (fail closed).
"""

from __future__ import annotations

import datetime
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tools.repository import REPOSITORY_ROOT

EXCEPTIONS = Path("tools/ci/audit-exceptions.toml")
NPM_FAILING = frozenset({"high", "critical"})


class AuditError(Exception):
    """The audit report or the exceptions cannot be read."""


def exceptions(path: Path, kind: str, today: datetime.date) -> tuple[dict[str, str], list[str]]:
    """({advisory: package}, failures) of the recorded exceptions of one ecosystem."""
    try:
        listed = tomllib.loads(path.read_text("utf-8")).get("exception", [])
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AuditError(f"cannot read {path}: {error}") from error
    found: dict[str, str] = {}
    failures = []
    for entry in listed:
        if entry.get("ecosystem") not in ("pip", "npm"):
            failures.append(f"AUDIT_EXCEPTION_INVALID {entry.get('id', '?')}: ecosystem pip or npm")
            continue
        if entry["ecosystem"] != kind:
            continue
        advisory, package = str(entry.get("id", "")), str(entry.get("package", ""))
        reason, review = str(entry.get("reason", "")).strip(), entry.get("review")
        if not advisory or not package or not reason or not isinstance(review, datetime.date):
            failures.append(
                f"AUDIT_EXCEPTION_INVALID {advisory or '?'}: needs id, package, reason, review"
            )
            continue
        if review < today:
            failures.append(
                f"AUDIT_EXCEPTION_EXPIRED {advisory}: its review date {review} has passed"
            )
            continue
        found[advisory] = package
    return found, failures


def pip_findings(report: object) -> list[tuple[str, str]]:
    """(package, advisory) of every vulnerability in a `pip-audit -f json` report."""
    try:
        parsed: Any = report
        return [
            (str(dependency["name"]), str(vulnerability["id"]))
            for dependency in parsed["dependencies"]
            for vulnerability in dependency.get("vulns", [])
        ]
    except (KeyError, TypeError) as error:
        raise AuditError(f"not a pip-audit report: {error}") from error


def npm_findings(report: object) -> list[tuple[str, str]]:
    """(package, advisory) of every high or critical advisory in an `npm audit --json` report."""
    try:
        parsed: Any = report
        found = []
        for name, entry in parsed["vulnerabilities"].items():
            for via in entry.get("via", []):
                if isinstance(via, dict) and via.get("severity") in NPM_FAILING:
                    advisory = str(via.get("url", "")).rsplit("/", 1)[-1] or str(via["source"])
                    found.append((str(name), advisory))
        return found
    except (KeyError, TypeError, AttributeError) as error:
        raise AuditError(f"not an npm audit report: {error}") from error


def judge(findings: Sequence[tuple[str, str]], allowed: dict[str, str]) -> list[str]:
    failures = [
        f"AUDIT_VULNERABLE {package}: {advisory}"
        for package, advisory in sorted(set(findings))
        if allowed.get(advisory) != package
    ]
    used = {advisory for _, advisory in findings}
    failures += [
        f"AUDIT_EXCEPTION_UNUSED {advisory}: {package} no longer has it; remove the exception"
        for advisory, package in sorted(allowed.items())
        if advisory not in used
    ]
    return failures


def main(argv: Sequence[str] | None = None, today: datetime.date | None = None) -> int:
    kind, report_path = list(sys.argv[1:] if argv is None else argv)
    if kind not in ("pip", "npm"):
        print(f"::error::AUDIT_REPORT_INVALID: unknown kind {kind!r}")
        return 1
    try:
        report = json.loads(Path(report_path).read_text("utf-8"))
        findings = pip_findings(report) if kind == "pip" else npm_findings(report)
        allowed, failures = exceptions(
            REPOSITORY_ROOT / EXCEPTIONS, kind, today or datetime.datetime.now(datetime.UTC).date()
        )
    except (OSError, ValueError, AuditError) as error:
        print(f"::error::AUDIT_REPORT_INVALID: {error}")
        return 1
    failures += judge(findings, allowed)
    for failure in failures:
        print(f"::error::{failure}")
    print(f"audit ({kind}): {len(findings)} known vulnerabilit(ies), {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
