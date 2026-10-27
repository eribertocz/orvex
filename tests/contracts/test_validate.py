import json
import shutil
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

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
