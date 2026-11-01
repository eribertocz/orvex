"""P0-11: the imported corpus, the pinned Ossie schema and the corpus runner.

Rejection cases run on copies in a temporary folder; the imported files are never edited.
"""

from __future__ import annotations

import json
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
