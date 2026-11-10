"""#429: a known vulnerability fails unless recorded with its reason and a review date."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from tools.ci import audit
from tools.repository import REPOSITORY_ROOT

TODAY = datetime.date(2026, 11, 10)


def pip_report(*vulnerable: tuple[str, str]) -> dict[str, object]:
    dependencies: list[dict[str, object]] = [{"name": "orvex-store", "skip_reason": "editable"}]
    dependencies += [{"name": n, "version": "1", "vulns": [{"id": i}]} for n, i in vulnerable]
    dependencies.append({"name": "clean", "version": "1", "vulns": []})
    return {"dependencies": dependencies}


def npm_report(severity: str) -> dict[str, object]:
    via = {"source": 1, "severity": severity, "url": "https://github.com/advisories/GHSA-aaaa"}
    return {"vulnerabilities": {"left-pad": {"via": [via, "other-package"]}}}


def write_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    (tmp_path / "tools/ci").mkdir(parents=True)
    (tmp_path / audit.EXCEPTIONS).write_text(text, "utf-8")
    monkeypatch.setattr(audit, "REPOSITORY_ROOT", tmp_path)


def run(tmp_path: Path, kind: str, report: object) -> int:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), "utf-8")
    return audit.main([kind, str(path)], today=TODAY)


EXCEPTION = (
    '[[exception]]\necosystem = "{kind}"\npackage = "{package}"\nid = "{id}"\n'
    'reason = "not reachable here"\nreview = {review}\n'
)


def test_a_vulnerability_fails_and_a_clean_report_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_exceptions(tmp_path, monkeypatch, "")
    assert run(tmp_path, "pip", pip_report()) == 0
    assert run(tmp_path, "pip", pip_report(("jinja2", "PYSEC-1"))) == 1
    assert run(tmp_path, "npm", npm_report("high")) == 1
    assert run(tmp_path, "npm", npm_report("critical")) == 1
    assert run(tmp_path, "npm", npm_report("low")) == 0


def test_a_recorded_exception_passes_until_its_review_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = EXCEPTION.format(kind="pip", package="jinja2", id="PYSEC-1", review="2026-12-31")
    write_exceptions(tmp_path, monkeypatch, entry)
    assert run(tmp_path, "pip", pip_report(("jinja2", "PYSEC-1"))) == 0
    assert run(tmp_path, "npm", npm_report("low")) == 0  # a pip exception is not npm's
    expired = EXCEPTION.format(kind="pip", package="jinja2", id="PYSEC-1", review="2026-11-01")
    (tmp_path / audit.EXCEPTIONS).write_text(expired, "utf-8")
    assert run(tmp_path, "pip", pip_report(("jinja2", "PYSEC-1"))) == 1
    assert "AUDIT_EXCEPTION_EXPIRED PYSEC-1" in capsys.readouterr().out


def test_an_exception_for_another_package_unused_or_incomplete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = EXCEPTION.format(kind="pip", package="flask", id="PYSEC-1", review="2026-12-31")
    write_exceptions(tmp_path, monkeypatch, other)
    assert run(tmp_path, "pip", pip_report(("jinja2", "PYSEC-1"))) == 1
    assert run(tmp_path, "pip", pip_report()) == 1
    assert "AUDIT_EXCEPTION_UNUSED PYSEC-1" in capsys.readouterr().out
    (tmp_path / audit.EXCEPTIONS).write_text(
        '[[exception]]\necosystem = "pip"\nid = "X"\n', "utf-8"
    )
    assert run(tmp_path, "pip", pip_report()) == 1
    assert "AUDIT_EXCEPTION_INVALID X" in capsys.readouterr().out


def test_an_npm_finding_matches_its_ghsa_exception_and_a_transitive_entry_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = EXCEPTION.format(kind="npm", package="left-pad", id="GHSA-aaaa", review="2026-12-31")
    write_exceptions(tmp_path, monkeypatch, entry)
    assert run(tmp_path, "npm", npm_report("high")) == 0
    transitive = {"vulnerabilities": {"wrapper": {"via": ["left-pad"]}}}
    assert audit.npm_findings(transitive) == []


def test_an_exception_without_a_reason_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = EXCEPTION.format(kind="pip", package="jinja2", id="PYSEC-1", review="2026-12-31")
    write_exceptions(tmp_path, monkeypatch, entry.replace("not reachable here", " "))
    assert run(tmp_path, "pip", pip_report(("jinja2", "PYSEC-1"))) == 1
    assert "AUDIT_EXCEPTION_INVALID PYSEC-1" in capsys.readouterr().out


def test_an_unreadable_report_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_exceptions(tmp_path, monkeypatch, "")
    assert run(tmp_path, "pip", {"no": "dependencies"}) == 1
    assert run(tmp_path, "npm", ["not", "a", "report"]) == 1
    assert audit.main(["pip", str(tmp_path / "missing.json")], today=TODAY) == 1
    assert audit.main(["yarn", str(tmp_path / "missing.json")], today=TODAY) == 1


def test_the_repository_records_no_stale_exception() -> None:
    _, failures = audit.exceptions(REPOSITORY_ROOT / audit.EXCEPTIONS, "pip", TODAY)
    assert failures == []
