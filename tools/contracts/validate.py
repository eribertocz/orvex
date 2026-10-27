"""Validate every contract: schema against its meta-schema, examples, and house rules.

    python -m tools.contracts.validate

Codes: CONTRACT_SCHEMA_INVALID, CONTRACT_EXAMPLE_REJECTED, CONTRACT_EXAMPLE_ACCEPTED,
CONTRACT_OPEN_OBJECT, CONTRACT_LAYOUT_INVALID.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from openapi_spec_validator import validate as validate_openapi
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tools.repository import REPOSITORY_ROOT

CONTRACTS = REPOSITORY_ROOT / "contracts"
_VERSION = re.compile(r"v[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.path}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ContractVersion:
    name: str
    version: str
    directory: Path

    @property
    def document(self) -> Path:
        schema = self.directory / "schema.json"
        return schema if schema.exists() else self.directory / "openapi.yaml"

    @property
    def is_openapi(self) -> bool:
        return self.document.name == "openapi.yaml"


def contract_versions(root: Path = CONTRACTS) -> Iterator[ContractVersion]:
    for directory in sorted(path for path in root.glob("*/v*") if path.is_dir()):
        yield ContractVersion(directory.parent.name, directory.name, directory)


def load_document(contract: ContractVersion) -> dict[str, Any]:
    text = contract.document.read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text) if contract.is_openapi else json.loads(text)
    return loaded


def open_objects(node: object, pointer: str = "#") -> Iterator[str]:
    """JSON pointers of object schemas that do not forbid additional properties."""
    if isinstance(node, dict):
        is_object = node.get("type") == "object" or ("properties" in node and "$ref" not in node)
        if is_object and node.get("additionalProperties") is not False:
            yield pointer
        for key, value in node.items():
            if key in {"examples", "example", "default", "const", "enum"}:
                continue
            yield from open_objects(value, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from open_objects(item, f"{pointer}/{index}")


def example_validator(document: dict[str, Any], schema_name: str | None) -> Draft202012Validator:
    if schema_name is None:
        return Draft202012Validator(document, format_checker=FormatChecker())
    registry: Registry[Any] = Registry().with_resource(
        "urn:contract", Resource.from_contents(document, default_specification=DRAFT202012)
    )
    return Draft202012Validator(
        {"$ref": f"urn:contract#/components/schemas/{schema_name}"},
        registry=registry,
        format_checker=FormatChecker(),
    )


def check_contract(contract: ContractVersion, root: Path) -> list[Finding]:
    relative = contract.directory.relative_to(root).as_posix()
    findings: list[Finding] = []
    if not _VERSION.fullmatch(contract.version):
        return [Finding("CONTRACT_LAYOUT_INVALID", relative, "version directory must be v<N>")]
    if not contract.document.exists():
        return [Finding("CONTRACT_LAYOUT_INVALID", relative, "no schema.json or openapi.yaml")]
    if not (contract.directory / "README.md").exists():
        findings.append(Finding("CONTRACT_LAYOUT_INVALID", relative, "README.md is missing"))
    document_path = contract.document.relative_to(root).as_posix()
    try:
        document = load_document(contract)
    except (ValueError, yaml.YAMLError) as error:
        return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, str(error))]

    if contract.is_openapi:
        try:
            validate_openapi(document)
        except OpenAPIValidationError as error:
            return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, error.message)]
        schemas = document.get("components", {}).get("schemas", {})
        targets = {name: f"#/components/schemas/{name}" for name in schemas}
        findings += [
            Finding("CONTRACT_OPEN_OBJECT", document_path, pointer)
            for pointer in open_objects(schemas, "#/components/schemas")
        ]
    else:
        try:
            validator_for(document).check_schema(document)
        except SchemaError as error:
            return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, error.message)]
        for field in ("$id", "title"):
            if field not in document:
                findings.append(
                    Finding("CONTRACT_SCHEMA_INVALID", document_path, f"{field} missing")
                )
        findings += [
            Finding("CONTRACT_OPEN_OBJECT", document_path, pointer)
            for pointer in open_objects(document)
        ]
        targets = {}

    for kind in ("valid", "invalid"):
        for example in sorted((contract.directory / "examples" / kind).glob("*.json")):
            findings += check_example(example, kind, document, targets, root)
    return findings


def check_example(
    example: Path, kind: str, document: dict[str, Any], targets: dict[str, str], root: Path
) -> list[Finding]:
    relative = example.relative_to(root).as_posix()
    schema_name = None
    if targets:
        schema_name = example.name.split(".", 1)[0]
        if schema_name not in targets:
            return [Finding("CONTRACT_LAYOUT_INVALID", relative, f"no schema '{schema_name}'")]
    errors = sorted(
        error.message
        for error in example_validator(document, schema_name).iter_errors(
            json.loads(example.read_text(encoding="utf-8"))
        )
    )
    if kind == "valid":
        return [Finding("CONTRACT_EXAMPLE_REJECTED", relative, "; ".join(errors))] if errors else []
    if not errors:
        return [Finding("CONTRACT_EXAMPLE_ACCEPTED", relative, "the invalid example validates")]
    expected_file = example.with_suffix(".expected.txt")
    if not expected_file.exists():
        return [Finding("CONTRACT_LAYOUT_INVALID", relative, "missing .expected.txt")]
    expected = expected_file.read_text(encoding="utf-8").strip()
    if not any(expected in error for error in errors):
        return [
            Finding(
                "CONTRACT_EXAMPLE_ACCEPTED",
                relative,
                f"rejected, but not for the stated reason '{expected}': {'; '.join(errors)}",
            )
        ]
    return []


def validate_all(root: Path = REPOSITORY_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for contract in contract_versions(root / "contracts"):
        findings += check_contract(contract, root)
    return findings


def main() -> int:
    findings = validate_all()
    for finding in findings:
        print(finding)
    print(f"contracts: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
