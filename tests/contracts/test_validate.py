import json
import shutil
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import tools.contracts.generate as generate
from tests.contracts.generated.example_v1 import ExampleReading
from tools.contracts.generate import python_types
from tools.contracts.validate import ContractVersion, contract_versions, validate_all
from tools.repository import REPOSITORY_ROOT

EXAMPLE = REPOSITORY_ROOT / "contracts" / "_example" / "v1"


def repository_with_example(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLE, tmp_path / "contracts" / "_example" / "v1")
    return tmp_path


def schema_of(root: Path) -> Path:
    return root / "contracts" / "_example" / "v1" / "schema.json"


def edit_schema(root: Path, change: dict[str, object]) -> None:
    path = schema_of(root)
    schema = json.loads(path.read_text())
    schema.update(change)
    path.write_text(json.dumps(schema))


def codes(root: Path) -> list[str]:
    return [finding.code for finding in validate_all(root)]


def test_repository_contracts_are_valid() -> None:
    assert validate_all() == []


def test_schema_breaking_its_meta_schema(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    edit_schema(root, {"type": "not-a-type"})
    assert codes(root) == ["CONTRACT_SCHEMA_INVALID"]
    assert [f.path for f in validate_all(root)] == ["contracts/_example/v1/schema.json"]


def test_valid_example_that_does_not_validate(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    (root / "contracts/_example/v1/examples/valid/broken.json").write_text('{"reading_id": ""}')
    assert codes(root) == ["CONTRACT_EXAMPLE_REJECTED"]


def test_invalid_example_that_validates(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    valid = root / "contracts/_example/v1/examples/valid/complete.json"
    shutil.copy(valid, root / "contracts/_example/v1/examples/invalid/passes.json")
    (root / "contracts/_example/v1/examples/invalid/passes.expected.txt").write_text("anything")
    assert codes(root) == ["CONTRACT_EXAMPLE_ACCEPTED"]


def test_invalid_example_rejected_for_another_reason(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    (root / "contracts/_example/v1/examples/invalid/bad-enum.expected.txt").write_text("nope")
    assert codes(root) == ["CONTRACT_EXAMPLE_ACCEPTED"]


def test_open_object(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    del schema["$defs"]["Consumer"]["additionalProperties"]
    path.write_text(json.dumps(schema))
    findings = {(f.code, f.detail) for f in validate_all(root)}
    assert ("CONTRACT_OPEN_OBJECT", "#/$defs/Consumer") in findings
    # The invalid example that relied on the closed object is now accepted, and reported too.
    assert ("CONTRACT_EXAMPLE_ACCEPTED", "the invalid example validates") in findings


def test_missing_id_and_bad_version_directory(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    del schema["$id"]
    path.write_text(json.dumps(schema))
    shutil.copytree(EXAMPLE, root / "contracts" / "_example" / "version1")
    assert sorted(codes(root)) == ["CONTRACT_LAYOUT_INVALID", "CONTRACT_SCHEMA_INVALID"]


def example_contract() -> ContractVersion:
    return next(c for c in contract_versions() if c.name == "_example")


def test_python_generation_is_deterministic() -> None:
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        assert python_types(example_contract(), Path(first)) == python_types(
            example_contract(), Path(second)
        )


def test_generated_decimal_is_a_string_never_a_number() -> None:
    example = json.loads((EXAMPLE / "examples/valid/complete.json").read_text())
    reading = ExampleReading.model_validate(example)
    assert reading.kwh.root == "123.4500"
    with pytest.raises(ValidationError):
        ExampleReading.model_validate(example | {"kwh": 1.5})
    with pytest.raises(ValidationError):
        ExampleReading.model_validate(example | {"kwh": "1e3"})


@pytest.mark.parametrize(
    "example", sorted((EXAMPLE / "examples").glob("*/*.json")), ids=lambda path: path.name
)
def test_generated_python_model_agrees_with_the_schema(example: Path) -> None:
    data = json.loads(example.read_text())
    if example.parent.name == "valid":
        ExampleReading.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            ExampleReading.model_validate(data)


# --- review findings: open objects, dialect, layout, references, OpenAPI -----------------


@pytest.mark.parametrize(
    "extra_property",
    [
        {"default": {"type": "object", "properties": {"a": {"type": "string"}}}},
        {"enum": {"type": "object", "properties": {"a": {"type": "string"}}}},
        {"maybe": {"type": ["object", "null"]}},
        {"anything": {}},
        {"required_only": {"required": ["a"]}},
        {
            "ref_with_siblings": {
                "$ref": "#/$defs/Consumer",
                "properties": {"x": {"type": "string"}},
            }
        },
        {"in_list": {"type": "array", "items": {"type": "object"}}},
        {"in_all_of": {"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]}},
        {"not_alone": {"not": {"type": "string"}}},
        {"any_of_not": {"anyOf": [{"type": "string"}, {"not": {"type": "string"}}]}},
        {"items_true": {"type": "array", "items": True}},
        {"array_without_items": {"type": "array"}},
        {"nullable_array": {"type": ["array", "null"]}},
        {"prefix_items_open_tail": {"type": "array", "prefixItems": [{"type": "string"}]}},
        {"contains_only": {"type": "array", "contains": {"type": "string"}}},
    ],
    ids=lambda case: next(iter(case)),
)
def test_open_object_shapes_are_reported(tmp_path: Path, extra_property: dict[str, object]) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    schema["properties"].update(extra_property)
    path.write_text(json.dumps(schema))
    assert "CONTRACT_OPEN_OBJECT" in codes(root)


@pytest.mark.parametrize("container", ["$def", "x-any", "default"])
def test_open_object_reached_only_through_ref_is_reported(tmp_path: Path, container: str) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    schema[container] = {"Meter": {"type": "object", "properties": {"serial": {"type": "string"}}}}
    schema["properties"]["meter"] = {"$ref": f"#/{container}/Meter"}
    path.write_text(json.dumps(schema))
    findings = [(f.code, f.detail) for f in validate_all(root)]
    assert ("CONTRACT_OPEN_OBJECT", f"#/{container}/Meter") in findings


def test_open_object_is_reported_once(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    del schema["$defs"]["Consumer"]["additionalProperties"]
    path.write_text(json.dumps(schema))
    details = [f.detail for f in validate_all(root) if f.code == "CONTRACT_OPEN_OBJECT"]
    assert details == ["#/$defs/Consumer"]


def test_true_schema_is_open(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    schema["properties"]["loose"] = True
    path.write_text(json.dumps(schema))
    assert "CONTRACT_OPEN_OBJECT" in codes(root)


@pytest.mark.parametrize(
    "dialect",
    ["http://json-schema.org/draft-07/schema#", "https://example.invalid/x", None],
)
def test_only_json_schema_2020_12_is_accepted(tmp_path: Path, dialect: str | None) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    if dialect is None:
        del schema["$schema"]
    else:
        schema["$schema"] = dialect
    path.write_text(json.dumps(schema))
    assert "CONTRACT_SCHEMA_INVALID" in codes(root)


def test_non_local_reference_is_rejected(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    schema["$defs"]["Remote"] = {"$ref": "https://example.invalid/schema.json"}
    path.write_text(json.dumps(schema))
    assert codes(root) == ["CONTRACT_SCHEMA_INVALID"]


def test_empty_expected_text_is_a_layout_error(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    (root / "contracts/_example/v1/examples/invalid/bad-enum.expected.txt").write_text("  \n")
    assert codes(root) == ["CONTRACT_LAYOUT_INVALID"]


@pytest.mark.parametrize("kind", ["valid", "invalid"])
def test_each_kind_of_example_is_required(tmp_path: Path, kind: str) -> None:
    root = repository_with_example(tmp_path)
    shutil.rmtree(root / "contracts/_example/v1/examples" / kind)
    assert codes(root) == ["CONTRACT_LAYOUT_INVALID"]


def test_example_that_is_not_json_is_a_layout_error(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    (root / "contracts/_example/v1/examples/valid/broken.json").write_text("{not json")
    assert codes(root) == ["CONTRACT_LAYOUT_INVALID"]


def test_contract_outside_a_version_directory_is_reported(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    (root / "contracts/other").mkdir()
    (root / "contracts/other/schema.json").write_text("{}")
    (root / "contracts/stray.json").write_text("{}")
    assert sorted(codes(root)) == ["CONTRACT_LAYOUT_INVALID"] * 3


OPENAPI = """openapi: {version}
info: {{title: t, version: v1}}
paths:
  /x:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {inline}
components:
  schemas:
    Thing:
      type: object
      additionalProperties: false
      properties: {{name: {{type: string}}}}
"""


def openapi_repository(tmp_path: Path, version: str, inline: str) -> Path:
    directory = tmp_path / "contracts" / "thing" / "v1"
    (directory / "examples" / "valid").mkdir(parents=True)
    (directory / "examples" / "invalid").mkdir(parents=True)
    (directory / "README.md").write_text("# thing\n")
    (directory / "openapi.yaml").write_text(OPENAPI.format(version=version, inline=inline))
    (directory / "examples/valid/Thing.ok.json").write_text('{"name": "a"}')
    (directory / "examples/invalid/Thing.extra.json").write_text('{"name": "a", "x": 1}')
    (directory / "examples/invalid/Thing.extra.expected.txt").write_text("Additional properties")
    return tmp_path


def test_openapi_contract_with_closed_schemas_is_valid(tmp_path: Path) -> None:
    root = openapi_repository(tmp_path, "3.1.0", "{$ref: '#/components/schemas/Thing'}")
    assert codes(root) == []


def test_openapi_3_0_is_rejected(tmp_path: Path) -> None:
    root = openapi_repository(tmp_path, "3.0.3", "{$ref: '#/components/schemas/Thing'}")
    assert codes(root) == ["CONTRACT_SCHEMA_INVALID"]


def test_open_inline_schema_under_paths_is_reported(tmp_path: Path) -> None:
    root = openapi_repository(tmp_path, "3.1.0", "{type: object}")
    findings = validate_all(root)
    assert [f.code for f in findings] == ["CONTRACT_OPEN_OBJECT"]
    assert findings[0].detail.startswith("#/paths/")


def test_generated_files_on_disk_include_orphans_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = tmp_path / "web"
    package = tmp_path / "packages/contracts/src/ns/contracts"
    monkeypatch.setattr(generate, "WEB", web)
    monkeypatch.setattr(generate, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(generate, "namespace_contracts", lambda: package)
    generated = [
        web / "src/lib/contracts/thing/v1.ts",
        package / "thing/v1.py",
        package / "thing/__init__.py",
        tmp_path / "tests/contracts/generated/example_v1.py",
    ]
    hand_written = [
        web / "src/lib/contracts/thing/v1.test.ts",
        web / "src/lib/contracts/contracts.test.ts",
        package / "__init__.py",
        tmp_path / "tests/contracts/generated/__init__.py",
    ]
    for path in generated + hand_written:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    assert generate.generated_files_on_disk() == set(generated)


@pytest.mark.parametrize("name", ["Bad Name", "1x", "a_b", "Upper"])
def test_contract_names_must_be_kebab_case(tmp_path: Path, name: str) -> None:
    root = repository_with_example(tmp_path)
    shutil.copytree(root / "contracts/_example", root / "contracts" / name)
    assert "CONTRACT_LAYOUT_INVALID" in codes(root)


def test_both_documents_in_one_version_is_a_layout_error(tmp_path: Path) -> None:
    root = repository_with_example(tmp_path)
    (root / "contracts/_example/v1/openapi.yaml").write_text("openapi: 3.1.0\n")
    assert "CONTRACT_LAYOUT_INVALID" in codes(root)


@pytest.mark.parametrize(
    "stray",
    [
        "examples/invalid/orphan.expected.txt",
        "examples/extra/a.json",
        "examples/valid/notes.txt",
        "examples/top.json",
    ],
)
def test_stray_example_files_are_reported(tmp_path: Path, stray: str) -> None:
    root = repository_with_example(tmp_path)
    path = root / "contracts/_example/v1" / stray
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert "CONTRACT_LAYOUT_INVALID" in codes(root)


def test_openapi_example_for_unknown_schema_is_reported(tmp_path: Path) -> None:
    root = openapi_repository(tmp_path, "3.1.0", "{$ref: '#/components/schemas/Thing'}")
    examples = root / "contracts/thing/v1/examples/valid"
    (examples / "Anything.ok.json").write_text("{}")
    assert codes(root) == ["CONTRACT_LAYOUT_INVALID"]


@pytest.mark.parametrize("reference", ["#/$defs/Missing", "#meter"])
def test_dangling_or_anchor_reference_is_rejected(tmp_path: Path, reference: str) -> None:
    root = repository_with_example(tmp_path)
    path = schema_of(root)
    schema = json.loads(path.read_text())
    schema["properties"]["meter"] = {"$ref": reference}
    path.write_text(json.dumps(schema))
    assert codes(root) == ["CONTRACT_SCHEMA_INVALID"]
