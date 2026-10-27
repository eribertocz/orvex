"""P0-11: the imported corpus, the pinned Ossie schema and the corpus runner.

Rejection cases run on copies in a temporary folder; the imported files are never edited.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from conformance.runners import bi_platform_adapter as adapter
from conformance.runners.semantic_corpus import CORPUS, OSSIE, ROOT, check_vendored, run


def copy_repository(tmp_path: Path) -> Path:
    for part in (CORPUS, OSSIE):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def codes(root: Path) -> list[str]:
    return sorted({finding.code for finding in run(root)[1]})


def test_every_case_loads_and_is_not_run() -> None:
    reports, findings = run()
    assert findings == []
    assert len(reports) == 21
    assert {(report.status, report.reason) for report in reports} == {
        ("not-run", "ENGINE_NOT_AVAILABLE")
    }
    assert sum(report.expected == "result" for report in reports) == 18
    assert sum(report.expected == "error" for report in reports) == 3
    assert {report.model for report in reports} == {"valid"}


def test_expected_errors_map_to_product_codes() -> None:
    mapped = {report.case: report.error_code for report in run()[0] if report.expected == "error"}
    assert mapped == {
        "CASE-016": "AMBIGUOUS_RELATIONSHIP_PATH",
        "CASE-017": "INCOMPATIBLE_UNITS",
        "CASE-020": "ILLEGAL_TEMPORAL_REAGGREGATION",
    }


def test_imported_files_match_source_toml() -> None:
    source = tomllib.loads((ROOT / CORPUS / "SOURCE.toml").read_text("utf-8"))
    assert source["commit"] == "072bb2c7502ae0f46a60a3156724a07e058b3d3d"
    assert len(source["sha256"]) == 124
    assert sum(name.startswith("corpus/") for name in source["sha256"]) == 82
    assert sum(name.startswith("models/") for name in source["sha256"]) == 42


def test_ossie_pin() -> None:
    pin = tomllib.loads((ROOT / OSSIE / "PIN.toml").read_text("utf-8"))
    assert pin["commit"] == "c721ffce76869cf941a1d9d8b9d01b607a87e838"
    schema = json.loads((ROOT / OSSIE / "ossie-schema.json").read_text("utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["version"]["const"] == pin["specification_version"] == "0.2.0.dev0"
    assert sorted(pin["sha256"]) == ["LICENSE", "NOTICE", "ossie-schema.json"]
    assert check_vendored(ROOT / OSSIE) == []


def test_report_is_deterministic(tmp_path: Path) -> None:
    outputs = []
    for name in ("first.json", "second.json"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "conformance.runners.semantic_corpus",
                "--json",
                str(tmp_path / name),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout
        outputs.append((tmp_path / name).read_bytes())
    assert outputs[0] == outputs[1]


def test_empty_csv_field_is_null_and_decimals_keep_scale() -> None:
    cases = ROOT / CORPUS / "corpus" / "cases"
    measurements = adapter.read_csv(cases / "CASE-014" / "input" / "measurements.csv")
    assert {"measurement_id": "2", "amount": None} in measurements
    result = adapter.read_csv(cases / "CASE-019" / "expected" / "result.csv")
    assert result[0]["revenue"] == "0.1000"


def test_vendor_alias_is_recognized() -> None:
    model = adapter.load_yaml(ROOT / CORPUS / "models" / "CASE-016.yaml")
    payloads = adapter.vendor_extensions(model)
    assert len(payloads) == 6
    other: adapter.Json = {"custom_extensions": [{"vendor_name": "OTHER", "data": "{}"}]}
    assert adapter.vendor_extensions(other) == []


def case_folder(root: Path, case: str) -> Path:
    return root / CORPUS / "corpus" / "cases" / case


def refresh_sources(root: Path) -> None:
    """Re-record SHA-256 values so that a test exercises one defect, not the source check."""
    import hashlib

    corpus = root / CORPUS
    source = tomllib.loads((corpus / "SOURCE.toml").read_text("utf-8"))
    lines = ["[sha256]"]
    for path in sorted(p for p in corpus.rglob("*") if p.is_file() and p.name != "SOURCE.toml"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f'"{path.relative_to(corpus).as_posix()}" = "{digest}"')
    assert "sha256" in source
    (corpus / "SOURCE.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_missing_expected_is_incomplete(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    (case_folder(root, "CASE-001") / "expected" / "result.csv").unlink()
    refresh_sources(root)
    assert codes(root) == ["CORPUS_CASE_INCOMPLETE"]


def test_both_expected_files_are_incomplete(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    (case_folder(root, "CASE-001") / "expected" / "error.json").write_text("{}", encoding="utf-8")
    refresh_sources(root)
    assert codes(root) == ["CORPUS_CASE_INCOMPLETE"]


def test_manifest_disagreement(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    path = root / CORPUS / "corpus" / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["cases"][0]["outcome"] = "compile_error"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    refresh_sources(root)
    assert codes(root) == ["CORPUS_MANIFEST_MISMATCH"]


def test_invalid_model(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    path = root / CORPUS / "models" / "CASE-001.yaml"
    path.write_text(
        path.read_text("utf-8").replace("version: '0.2.0.dev0'", "version: '9'"), "utf-8"
    )
    refresh_sources(root)
    assert codes(root) == ["CORPUS_MODEL_INVALID"]


def test_unmapped_error_code(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    path = case_folder(root, "CASE-016") / "expected" / "error.json"
    path.write_text(json.dumps({"code": "SOMETHING_NEW", "evidence_contains": []}), "utf-8")
    refresh_sources(root)
    assert codes(root) == ["CORPUS_ERROR_CODE_UNMAPPED"]


@pytest.mark.parametrize("change", ["edit", "add", "nested-source-toml", "remove"])
def test_changed_source_is_detected(tmp_path: Path, change: str) -> None:
    root = copy_repository(tmp_path)
    folder = case_folder(root, "CASE-001")
    if change == "edit":
        path = folder / "README.md"
        path.write_text(path.read_text("utf-8") + "\n", encoding="utf-8")
    elif change == "add":
        (folder / "notes.md").write_text("x", encoding="utf-8")
    elif change == "nested-source-toml":
        (folder / "SOURCE.toml").write_text("x", encoding="utf-8")
    else:
        (folder / "README.md").unlink()
    assert "CORPUS_SOURCE_CHANGED" in codes(root)


@pytest.mark.parametrize("change", ["edit-schema", "add", "remove-license", "remove-schema"])
def test_changed_vendored_ossie_file_is_detected(tmp_path: Path, change: str) -> None:
    root = copy_repository(tmp_path)
    folder = root / OSSIE
    if change == "edit-schema":
        path = folder / "ossie-schema.json"
        path.write_text(path.read_text("utf-8") + "\n", encoding="utf-8")
    elif change == "add":
        (folder / "extra.json").write_text("{}", encoding="utf-8")
    elif change == "remove-license":
        (folder / "LICENSE").unlink()
    else:
        (folder / "ossie-schema.json").unlink()
    assert codes(root) == ["CORPUS_SOURCE_CHANGED"]


def test_case_missing_from_manifest(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    path = root / CORPUS / "corpus" / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["cases"] = manifest["cases"][:-1]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    refresh_sources(root)
    assert codes(root) == ["CORPUS_MANIFEST_MISMATCH"]


def test_malformed_expected_error_is_incomplete(tmp_path: Path) -> None:
    root = copy_repository(tmp_path)
    (case_folder(root, "CASE-016") / "expected" / "error.json").write_text("{", encoding="utf-8")
    refresh_sources(root)
    assert codes(root) == ["CORPUS_CASE_INCOMPLETE"]


def break_incomplete(root: Path) -> None:
    (case_folder(root, "CASE-001") / "expected" / "result.csv").unlink()


def break_manifest(root: Path) -> None:
    path = root / CORPUS / "corpus" / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["cases"][0]["outcome"] = "compile_error"
    path.write_text(json.dumps(manifest), encoding="utf-8")


def break_model(root: Path) -> None:
    path = root / CORPUS / "models" / "CASE-001.yaml"
    path.write_text(
        path.read_text("utf-8").replace("version: '0.2.0.dev0'", "version: '9'"), "utf-8"
    )


def break_error_code(root: Path) -> None:
    path = case_folder(root, "CASE-016") / "expected" / "error.json"
    path.write_text(json.dumps({"code": "SOMETHING_NEW", "evidence_contains": []}), "utf-8")


def break_source(root: Path) -> None:
    (case_folder(root, "CASE-001") / "README.md").write_text("changed", encoding="utf-8")


@pytest.mark.parametrize(
    ("defect", "line"),
    [
        (break_incomplete, "CORPUS_CASE_INCOMPLETE: CASE-001: "),
        (break_manifest, "CORPUS_MANIFEST_MISMATCH: CASE-001: "),
        (break_model, "CORPUS_MODEL_INVALID: CASE-001: "),
        (break_error_code, "CORPUS_ERROR_CODE_UNMAPPED: CASE-016: "),
        (break_source, "CORPUS_SOURCE_CHANGED: corpus/cases/CASE-001/README.md: "),
    ],
)
def test_command_exits_one_with_code_and_case(
    tmp_path: Path, defect: Callable[[Path], None], line: str
) -> None:
    root = copy_repository(tmp_path)
    defect(root)
    if defect is not break_source:
        refresh_sources(root)
    result = subprocess.run(
        [sys.executable, "-m", "conformance.runners.semantic_corpus", "--root", str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert line in result.stdout
