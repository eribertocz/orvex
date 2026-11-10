"""P0-11: the imported corpus, the pinned Ossie schema and the corpus runner.

Rejection cases run on copies in a temporary folder; the imported files are never edited.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conformance.runners import bi_platform_adapter as adapter
from conformance.runners import corpus_execution as ce
from conformance.runners import semantic_corpus
from conformance.runners.semantic_corpus import CORPUS, OSSIE, ROOT, check_vendored, run
from tests.support.environment import unavailable


def copy_repository(tmp_path: Path) -> Path:
    for part in (CORPUS, OSSIE):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def codes(root: Path) -> list[str]:
    return sorted({finding.code for finding in run(root, execute=False)[1]})


PASSING = {
    "CASE-004",
    "CASE-009",
    "CASE-010",
    "CASE-011",
    "CASE-012",
    "CASE-013",
    "CASE-019",
    "CASE-017",
    "CASE-001",
    "CASE-002",
    "CASE-006",
    "CASE-007",
    "CASE-008",
    "CASE-020",
    "CASE-021",
    "CASE-003",
    "CASE-005",
    "CASE-014",
    "CASE-015",
    "CASE-016",
    "CASE-018",
}


def test_the_http_api_gives_every_case_the_library_s_outcome() -> None:
    """P2-27a (#363): every case asked through `POST /api/v1/semantic/query`, as JSON and as
    Arrow, ends exactly as through the library; nothing fails either way."""
    from conformance.runners.semantic_corpus import compare

    library, _ = run()
    http, findings = run(through="http")
    assert findings == []
    assert compare(library, http) == []
    assert {r.case for r in http if r.status == "pass"} == PASSING


def existing_ducklake_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing lake's PostgreSQL server: `CORPUS_POSTGRES`, or the store tests' one."""
    from conformance.runners import corpus_existing_ducklake as existing

    for name, fallback in (
        (existing.SERVER, "STORE_TEST_POSTGRES"),
        (existing.PASSWORD, "STORE_TEST_POSTGRES_PASSWORD"),
    ):
        if not os.environ.get(name) and os.environ.get(fallback):
            monkeypatch.setenv(name, os.environ[fallback])
    if not (os.environ.get(existing.SERVER) and os.environ.get(existing.PASSWORD)):
        unavailable(
            "no PostgreSQL configured for this run",
            "CI needs a PostgreSQL server for the existing lake's catalog",
        )


def switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    from orvex.server import development_switch
    from orvex.server.identity import load_identity

    if on:
        monkeypatch.setenv(development_switch.variable(load_identity()), "1")
    else:
        monkeypatch.delenv(development_switch.variable(load_identity()), raising=False)


def test_on_an_existing_ducklake_every_case_ends_as_through_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-30 (#399): with the development switch on, every case asked on an existing DuckLake
    with a PostgreSQL catalog ends exactly as through the library: the corpus parity."""
    from conformance.runners.semantic_corpus import compare

    existing_ducklake_server(monkeypatch)
    switch(monkeypatch, on=True)
    library, _ = run()
    existing, findings = run(through="existing-ducklake")
    assert findings == []
    assert compare(library, existing, "existing-ducklake") == []
    assert {r.case for r in existing if r.status == "pass"} == PASSING


def test_with_the_switch_off_a_case_on_an_existing_ducklake_is_refused_for_its_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conformance.runners.corpus_existing_ducklake import run_case

    existing_ducklake_server(monkeypatch)
    switch(monkeypatch, on=False)
    folder = case_folder(ROOT, "CASE-001")
    found = run_case("CASE-001", folder, ROOT / CORPUS / "models" / "CASE-001.yaml", None)
    assert (found.status, found.reason) == ("not-run", "route (another data route, P2-29)")


def test_every_case_is_answered_or_not_run_with_its_capability() -> None:
    """P2-12 (#311): real results for what the engine supports; every other case is `not-run`
    and names what it waits for. None fails, and a later rule moves cases into `PASSING`."""
    reports, findings = run()
    assert findings == []
    assert len(reports) == 21
    assert {r.case for r in reports if r.status == "pass"} == PASSING
    for report in reports:
        assert report.status in ("pass", "not-run"), (report.case, report.reason)
        if report.status == "not-run":
            assert re.search(
                r"\((C[0-9]|policy placement|S[0-9]|.*P2-[0-9]+|#[0-9]+)", report.reason
            ), report
    assert sum(report.expected == "result" for report in reports) == 18
    assert sum(report.expected == "error" for report in reports) == 3
    assert {report.model for report in reports} == {"valid"}


def test_a_runner_side_not_run_fails_the_required_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#422: a case the runner skips (`NOT_YET`) is a finding, and the command exits 1; only
    an engine's declared refusal may end a case `not-run`."""
    from conformance.runners import corpus_execution, semantic_corpus

    monkeypatch.setattr(corpus_execution, "NOT_YET", {"CASE-001": "skipped by the runner"})
    _, findings = run()
    assert [str(f) for f in findings] == [
        "CORPUS_CASE_NOT_RUN: CASE-001: library: skipped by the runner"
    ]
    monkeypatch.setattr(sys, "argv", ["semantic_corpus", "--through", "both"])
    assert semantic_corpus.main() == 1
    printed = capsys.readouterr().out
    assert "CORPUS_CASE_NOT_RUN: CASE-001: library: skipped by the runner" in printed
    assert "CORPUS_CASE_NOT_RUN: CASE-001: http: skipped by the runner" in printed


def test_a_declared_refusal_ends_not_run_without_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#422: the route's own refusal (the switch off) is the product refusing: `not-run`, and
    no finding of the runner's."""
    from conformance.runners.semantic_corpus import load_case, ossie_validator

    existing_ducklake_server(monkeypatch)
    switch(monkeypatch, on=False)
    manifest = json.loads((ROOT / CORPUS / "corpus/manifest.json").read_text("utf-8"))
    entry = next(e for e in manifest["cases"] if e["id"] == "CASE-001")
    report, findings = load_case(
        ROOT / CORPUS, entry, ossie_validator(ROOT / OSSIE), through="existing-ducklake"
    )
    assert report is not None
    assert (report.status, findings) == ("not-run", [])


def test_expected_errors_map_to_product_codes() -> None:
    mapped = {
        report.case: report.error_code
        for report in run(execute=False)[0]
        if report.expected == "error"
    }
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
    assert sorted(pin["sha256"]) == [
        "LICENSE",
        "NOTICE",
        "dialects.md",
        "expression_language.md",
        "ossie-schema.json",
        "spec.md",
        "vendors.md",
    ]
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


# --- Translation into ossie-extensions v1 (P2-01, #276) -----------------------------------------

MODELS = sorted((ROOT / CORPUS / "models").glob("CASE-0??.yaml"))
VENDOR = "TESTVENDOR"  # the product's own name is read from identity.toml at run time


def extension_payloads(node: object, vendor: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(node, dict):
        for entry in node.get("custom_extensions") or []:
            if isinstance(entry, dict) and entry.get("vendor_name") == vendor:
                found.append(json.loads(entry["data"]))
        for value in node.values():
            found.extend(extension_payloads(value, vendor))
    elif isinstance(node, list):
        for item in node:
            found.extend(extension_payloads(item, vendor))
    return found


def load_model(path: Path) -> dict[str, Any]:
    model = adapter.load_yaml(path)
    assert isinstance(model, dict)
    return dict(model)


def test_the_corpus_has_21_models() -> None:
    assert len(MODELS) == 21


@pytest.mark.parametrize("path", MODELS, ids=lambda path: path.stem)
def test_every_corpus_payload_translates_into_a_valid_v1_payload(path: Path) -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT / "contracts" / "ossie-extensions" / "v1" / "schema.json").read_text(encoding="utf-8")
    )
    model = adapter.load_yaml(path)
    assert isinstance(model, dict)
    translated, _ = adapter.translate(dict(model), VENDOR)
    payloads = extension_payloads(translated, VENDOR)
    assert payloads
    for payload in payloads:
        assert list(Draft202012Validator(schema).iter_errors(payload)) == [], payload
    assert extension_payloads(translated, adapter.VENDOR_NAME) == []


def test_row_policies_are_set_aside_for_p2_08() -> None:
    with_policies = [path for path in MODELS if adapter.translate(load_model(path), VENDOR)[1]]
    assert len(with_policies) == 1


@pytest.mark.parametrize(
    ("where", "payload"),
    [
        ("dataset", {"version": 1, "entity": "x", "colour": "red"}),
        ("field", {"version": 1, "time_role": "lunar"}),
        ("field", {"version": 1, "unit": {"meters": "m"}}),
        ("metric", {"version": 1, "aggregation": {"class": "mostly_additive"}}),
        ("dataset", {"version": 2, "entity": "x"}),
    ],
)
def test_an_untranslatable_key_or_value_fails(where: str, payload: dict[str, object]) -> None:
    entry = {"vendor_name": adapter.VENDOR_NAME, "data": json.dumps(payload)}
    field: dict[str, Any] = {"name": "f", "expression": {"sql": "f"}}
    dataset: dict[str, Any] = {"name": "d", "source": "d", "fields": [field]}
    model: dict[str, Any] = {"version": "0.2.0.dev0", "name": "m", "datasets": [dataset]}
    if where == "dataset":
        dataset["custom_extensions"] = [entry]
    elif where == "field":
        field["custom_extensions"] = [entry]
    else:
        model["metrics"] = [
            {"name": "x", "expression": {"sql": "SUM(d.f)"}, "custom_extensions": [entry]}
        ]
    with pytest.raises(adapter.UntranslatableKey):
        adapter.translate(model, VENDOR)


def test_a_bridge_moves_onto_its_bridge_dataset() -> None:
    translated, _ = adapter.translate(
        load_model(ROOT / CORPUS / "models" / "CASE-011.yaml"), VENDOR
    )
    bridge_dataset = next(d for d in translated["datasets"] if d["name"] == "order_campaign")
    (payload,) = extension_payloads(bridge_dataset, VENDOR)
    assert payload["bridge"] == {
        "left": "order",
        "right": "campaign",
        "weight": "allocation_weight",
        "total": "1.00",
        "tolerance": "0.00",
        "normalization_scope": {"entities": ["order"]},
    }


def translated_or_error(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    model = load_model(ROOT / CORPUS / "models" / "CASE-011.yaml")
    change(model)
    return adapter.translate(model, VENDOR)[0]


def set_payload(node: dict[str, Any], payload: dict[str, Any]) -> None:
    node["custom_extensions"] = [{"vendor_name": adapter.VENDOR_NAME, "data": json.dumps(payload)}]


def bridges(model: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(model["custom_extensions"][0]["data"])
    return data


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("bridge extra key", r"bridge: .*\['note'\]"),
        ("bridge left is not the relationship end", "is not where left_relationship ends"),
        ("allocation extra key", r"bridge allocation: .*\['rounding'\]"),
        ("keys extra key", r"dataset keys: .*\['x'\]"),
    ],
)
def test_nested_keys_and_repeated_values_are_checked(change: str, reason: str) -> None:
    def edit(model: dict[str, Any]) -> None:
        data = bridges(model)
        if change == "bridge extra key":
            data["bridges"][0]["note"] = "x"
        elif change == "bridge left is not the relationship end":
            data["bridges"][0]["left"] = "campaign"
        elif change == "allocation extra key":
            data["bridges"][0]["allocation"]["rounding"] = "half_up"
        else:
            orders = model["datasets"][0]
            set_payload(
                orders, {"version": 1, "keys": [{"entity": "o", "fields": ["order_id"], "x": 1}]}
            )
            return
        set_payload(model, data)

    with pytest.raises(adapter.UntranslatableKey, match=reason):
        translated_or_error(edit)


@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        (
            {"rule": "period_end", "order_by": [{"field": "b.d", "direction": "desc"}]},
            r"order_by: .*\['direction'\]",
        ),
        (
            {"rule": "period_end", "order_by": [{"field": "b.d"}], "ties": {"policy": "first"}},
            r"ties: .*\['policy'\]",
        ),
        (
            {"rule": "period_end", "order_by": [{"field": "b.d"}], "axis": {"dimension": "other"}},
            "is no period_end axis",
        ),
    ],
    ids=["order direction", "ties policy", "axis is no period_end axis"],
)
def test_a_period_end_detail_that_cannot_be_kept_fails(
    behavior: dict[str, Any], reason: str
) -> None:
    def edit(model: dict[str, Any]) -> None:
        model["metrics"] = [
            {
                "name": "balance",
                "expression": {"sql": "SUM(orders.order_total)"},
                "custom_extensions": [
                    {
                        "vendor_name": adapter.VENDOR_NAME,
                        "data": json.dumps(
                            {
                                "version": 1,
                                "aggregation": {
                                    "class": "semi_additive",
                                    "axes": [{"dimension": "snapshot", "behavior": "period_end"}],
                                },
                                "time_behavior": behavior,
                            }
                        ),
                    }
                ],
            }
        ]

    with pytest.raises(adapter.UntranslatableKey, match=reason):
        translated_or_error(edit)


def test_an_axis_with_both_a_dimension_and_an_entity_fails() -> None:
    def edit(model: dict[str, Any]) -> None:
        axis = {"dimension": "d", "entity": "e", "behavior": "forbidden"}
        model["metrics"] = [
            {
                "name": "m",
                "expression": {"sql": "SUM(orders.order_total)"},
                "custom_extensions": [
                    {
                        "vendor_name": adapter.VENDOR_NAME,
                        "data": json.dumps(
                            {
                                "version": 1,
                                "aggregation": {"class": "semi_additive", "axes": [axis]},
                            }
                        ),
                    }
                ],
            }
        ]

    with pytest.raises(adapter.UntranslatableKey, match=r"aggregation axis: .*\[.entity.\]"):
        translated_or_error(edit)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {
                "version": 1,
                "aggregation": {
                    "class": "non_additive",
                    "axes": [{"dimension": "region", "behavior": "forbidden"}],
                },
            },
            "must be recompute",
        ),
        ({"version": 1, "allocation_bridge": "unknown"}, "names no bridge"),
    ],
    ids=["non-additive axis that does not recompute", "unknown allocation bridge"],
)
def test_a_metric_declaration_that_has_no_faithful_translation_fails(
    payload: dict[str, Any], reason: str
) -> None:
    def edit(model: dict[str, Any]) -> None:
        metric = {"name": "m", "expression": {"sql": "SUM(orders.order_total)"}}
        set_payload(metric, payload)
        model["metrics"] = [metric]

    with pytest.raises(adapter.UntranslatableKey, match=reason):
        translated_or_error(edit)


def axis_metric(additivity_class: str, behavior: str) -> dict[str, Any]:
    metric = {"name": "m", "expression": {"sql": "SUM(orders.order_total)"}}
    axes = [{"dimension": "region", "behavior": behavior}]
    set_payload(metric, {"version": 1, "aggregation": {"class": additivity_class, "axes": axes}})
    return metric


@pytest.mark.parametrize(
    ("additivity_class", "behavior"),
    [
        ("fully_additive", "distinct_recompute"),
        ("fully_additive", "forbidden"),
        ("non_additive", "additive"),
    ],
)
def test_an_axis_a_metric_class_contradicts_fails(additivity_class: str, behavior: str) -> None:
    def edit(model: dict[str, Any]) -> None:
        model["metrics"] = [axis_metric(additivity_class, behavior)]

    with pytest.raises(adapter.UntranslatableKey, match=r"an axis of a \w+ metric must be"):
        translated_or_error(edit)


@pytest.mark.parametrize(
    ("additivity_class", "behavior", "additivity"),
    [
        ("fully_additive", "additive", "additive"),
        ("non_additive", "distinct_recompute", "non_additive"),
    ],
)
def test_an_axis_its_metric_class_implies_is_dropped(
    additivity_class: str, behavior: str, additivity: str
) -> None:
    def edit(model: dict[str, Any]) -> None:
        model["metrics"] = [axis_metric(additivity_class, behavior)]

    translated = translated_or_error(edit)
    assert extension_payloads(translated["metrics"][0], VENDOR) == [
        {"version": 1, "element": "metric", "additivity": additivity}
    ]


def test_an_entry_on_an_element_the_adapter_never_reads_fails() -> None:
    def edit(model: dict[str, Any]) -> None:
        set_payload(model["datasets"][0]["fields"][0].setdefault("dimension", {}), {"version": 1})

    with pytest.raises(adapter.UntranslatableKey, match="never reads"):
        translated_or_error(edit)


def test_two_fields_with_one_time_role_fail() -> None:
    def edit(model: dict[str, Any]) -> None:
        for field in model["datasets"][0]["fields"][:2]:
            set_payload(field, {"version": 1, "time_role": "event"})

    with pytest.raises(adapter.UntranslatableKey, match="two fields with time_role 'event'"):
        translated_or_error(edit)


def test_another_corpus_semantic_version_fails() -> None:
    def edit(model: dict[str, Any]) -> None:
        data = bridges(model)
        data["semantic_version"] = "9.9.9"
        set_payload(model, data)

    with pytest.raises(adapter.UntranslatableKey, match=re.escape("semantic_version '9.9.9'")):
        translated_or_error(edit)


def test_two_bridges_on_one_bridge_dataset_fail() -> None:
    def edit(model: dict[str, Any]) -> None:
        data = bridges(model)
        data["bridges"].append({**data["bridges"][0], "name": "second"})
        set_payload(model, data)

    with pytest.raises(adapter.UntranslatableKey, match="two bridges on the bridge dataset"):
        translated_or_error(edit)


def test_a_payload_that_repeats_a_key_fails() -> None:
    def edit(model: dict[str, Any]) -> None:
        field = model["datasets"][0]["fields"][0]
        text = '{"version": 1, "dimension": "a", "dimension": "b"}'
        field["custom_extensions"] = [{"vendor_name": adapter.VENDOR_NAME, "data": text}]

    with pytest.raises(adapter.UntranslatableKey, match=re.escape("repeats a key: ['dimension']")):
        translated_or_error(edit)


@pytest.mark.parametrize("kind", ["bridge", "dataset", "relationship"])
def test_a_repeated_name_fails_instead_of_replacing_an_element(kind: str) -> None:
    def edit(model: dict[str, Any]) -> None:
        if kind == "bridge":
            data = bridges(model)
            data["bridges"].append({**data["bridges"][0], "bridge": "orders"})
            set_payload(model, data)
        elif kind == "dataset":
            model["datasets"].append(copy.deepcopy(model["datasets"][0]))
        else:
            model["relationships"].append(copy.deepcopy(model["relationships"][0]))

    with pytest.raises(adapter.UntranslatableKey, match=f"two {kind}s? named"):
        translated_or_error(edit)


# --- the exact set of cases per way (#427) ------------------------------------------------------


def _report(case: str, status: str = "pass") -> semantic_corpus.CaseReport:
    return semantic_corpus.CaseReport(case, "t", "result", None, "valid", status, "")


MANIFEST = {"cases": [{"id": "CASE-001"}, {"id": "CASE-002"}]}


@pytest.mark.parametrize(
    ("reports", "code"),
    [
        ([_report("CASE-001")], "CORPUS_CASE_MISSING"),
        ([_report("CASE-001"), _report("CASE-002"), _report("CASE-002")], "CORPUS_CASE_DUPLICATE"),
        ([_report("CASE-001"), _report("CASE-002"), _report("CASE-999")], "CORPUS_CASE_UNKNOWN"),
        ([_report("CASE-001"), _report("CASE-002", "not-run")], "CORPUS_CASE_NOT_PASSED"),
        ([_report("CASE-001"), _report("CASE-002", "fail")], "CORPUS_CASE_NOT_PASSED"),
    ],
    ids=["missing", "duplicate", "unknown", "refused or not run", "failed"],
)
def test_any_case_not_passed_exactly_once_fails_the_way(
    reports: list[semantic_corpus.CaseReport], code: str
) -> None:
    found = semantic_corpus.exact_cases(MANIFEST, reports, "clickhouse", {})
    assert [f.code for f in found] == [code]
    complete = [_report("CASE-001"), _report("CASE-002")]
    assert semantic_corpus.exact_cases(MANIFEST, complete, "clickhouse", {}) == []


def test_a_known_gap_must_fail_and_must_be_due_later(tmp_path: Path) -> None:
    gap = {"CASE-002": "#9"}
    failing = [_report("CASE-001"), _report("CASE-002", "not-run")]
    assert semantic_corpus.exact_cases(MANIFEST, failing, "library", gap) == []
    passing = [_report("CASE-001"), _report("CASE-002")]
    found = semantic_corpus.exact_cases(MANIFEST, passing, "library", gap)
    assert [f.code for f in found] == ["CORPUS_EXPECTED_FAIL_PASSED"]
    (tmp_path / "expected-fail.toml").write_text(
        '"CASE-001" = {issue = 9, due = "P3"}\n"CASE-002" = {issue = 9, due = "P2"}\n'
        '"CASE-003" = {due = "P5"}\n',
        "utf-8",
    )
    gaps, findings = semantic_corpus.expected_failures(tmp_path, minor=3)
    assert gaps == {"CASE-001": "#9"}
    assert [(f.code, f.case) for f in findings] == [
        ("CORPUS_EXPECTED_FAIL_OVERDUE", "CASE-002"),
        ("CORPUS_EXPECTED_FAIL_INVALID", "CASE-003"),
    ]


def test_a_rolled_up_cell_holding_a_value_fails_its_case() -> None:
    found = ce.Answered(
        envelope={
            "columns": [{"role": "dimension"}, {"role": "metric"}],
            "row_roles": [{"role": "detail", "level": 1}, {"role": "total", "level": 0}],
        },
        rows=[["a", "1"], ["b", "1"]],
    )
    assert ce.totals_written(found) == [["a", "1"], ["ROLLED-UP CELL WITH A VALUE: b", "1"]]
