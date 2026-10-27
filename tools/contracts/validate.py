"""Validate every contract: layout, schema against its meta-schema, house rules and examples.

    python -m tools.contracts.validate

Codes: CONTRACT_SCHEMA_INVALID, CONTRACT_EXAMPLE_REJECTED, CONTRACT_EXAMPLE_ACCEPTED (the
invalid example validates, or is rejected for another reason than the stated one),
CONTRACT_OPEN_OBJECT, CONTRACT_LAYOUT_INVALID. The generator adds CONTRACT_TYPES_STALE and
CONTRACT_TOOLING_MISSING.
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
from openapi_spec_validator import validate as validate_openapi
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from tools.repository import REPOSITORY_ROOT

CONTRACTS = REPOSITORY_ROOT / "contracts"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_VERSION = re.compile(r"v[1-9][0-9]*")
_NAME = re.compile(r"_?[a-z][a-z0-9]*(-[a-z0-9]+)*")

# Keywords whose value is one schema, a list of schemas, or a map of name -> schema.
_SCHEMA_KEYWORDS = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
)
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAP_KEYWORDS = (
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
)
_OBJECT_KEYWORDS = (
    "properties",
    "required",
    "patternProperties",
    "propertyNames",
    "minProperties",
    "maxProperties",
    "dependentRequired",
)
# A schema with one of these is constrained even without `type`.
# `not` alone does not constrain: `{"not": {"type": "string"}}` still accepts any object.
_CONSTRAINING_KEYWORDS = ("type", "$ref", "const", "enum", *_SCHEMA_LIST_KEYWORDS)


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
    for directory in sorted(path for path in root.glob("*/*") if path.is_dir()):
        if _VERSION.fullmatch(directory.name):
            yield ContractVersion(directory.parent.name, directory.name, directory)


def load_document(contract: ContractVersion) -> dict[str, Any]:
    text = contract.document.read_text(encoding="utf-8")
    loaded: object = yaml.safe_load(text) if contract.is_openapi else json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("the contract document is not an object")
    return loaded


def resolve_pointer(document: object, reference: str) -> object:
    """The value at a local JSON pointer (`#/a/b`), or None when it does not exist."""
    node = document
    for raw in reference.removeprefix("#").split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def open_objects(
    schema: object,
    pointer: str = "#",
    document: object = None,
    visited: set[str] | None = None,
) -> Iterator[str]:
    """JSON pointers of schemas that accept objects with undeclared properties.

    The walk follows keyword positions (never key names), so a property called `default` or
    `enum` is checked like any other, and it follows every local `$ref` to its target, wherever
    the target lives in the document (each target is walked once).
    """
    document = schema if document is None else document
    visited = set() if visited is None else visited
    if schema is True:
        yield pointer
        return
    if not isinstance(schema, dict):
        return
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#") and reference not in visited:
        visited.add(reference)
        yield from open_objects(resolve_pointer(document, reference), reference, document, visited)
    types = schema.get("type")
    type_list = types if isinstance(types, list) else [types]
    is_object = (
        "object" in type_list
        or any(keyword in schema for keyword in _OBJECT_KEYWORDS)
        or not any(keyword in schema for keyword in _CONSTRAINING_KEYWORDS)
    )
    if is_object and schema.get("additionalProperties") is not False:
        yield pointer
    is_array = "array" in type_list or "prefixItems" in schema or "contains" in schema
    if is_array and "items" not in schema:
        # A missing `items` means any element, objects included.
        yield f"{pointer}/items"
    for keyword in _SCHEMA_KEYWORDS:
        child = schema.get(keyword)
        if child is not None:
            yield from open_objects(child, f"{pointer}/{keyword}", document, visited)
    for keyword in _SCHEMA_LIST_KEYWORDS:
        for index, child in enumerate(schema.get(keyword) or []):
            yield from open_objects(child, f"{pointer}/{keyword}/{index}", document, visited)
    for keyword in _SCHEMA_MAP_KEYWORDS:
        for name, child in (schema.get(keyword) or {}).items():
            yield from open_objects(child, f"{pointer}/{keyword}/{name}", document, visited)


def openapi_schemas(document: object, pointer: str = "#") -> Iterator[tuple[str, object]]:
    """Every schema in an OpenAPI document: component schemas and every inline `schema`."""
    if isinstance(document, dict):
        for key, value in document.items():
            child = f"{pointer}/{key}"
            if pointer == "#/components" and key == "schemas" and isinstance(value, dict):
                yield from ((f"{child}/{name}", schema) for name, schema in value.items())
            elif key == "schema":
                yield child, value
            else:
                yield from openapi_schemas(value, child)
    elif isinstance(document, list):
        for index, item in enumerate(document):
            yield from openapi_schemas(item, f"{pointer}/{index}")


def unresolved_references(
    document: object, node: object = None, pointer: str = "#"
) -> Iterator[str]:
    """Local `$ref`s that are anchors (`#name`) or point to nothing in the document."""
    node = document if node is None else node
    if isinstance(node, dict):
        reference = node.get("$ref")
        is_local = isinstance(reference, str) and reference.startswith("#") and reference != "#"
        if is_local and (
            not str(reference).startswith("#/") or resolve_pointer(document, str(reference)) is None
        ):
            yield f"{pointer}: {reference}"
        for key, value in node.items():
            yield from unresolved_references(document, value, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from unresolved_references(document, item, f"{pointer}/{index}")


def non_local_references(node: object, pointer: str = "#") -> Iterator[str]:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            yield f"{pointer}: {reference}"
        for key, value in node.items():
            yield from non_local_references(value, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from non_local_references(item, f"{pointer}/{index}")


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


def check_layout(root: Path) -> list[Finding]:
    """Every entry under contracts/ is README.md or a contract with only v<N> directories."""
    contracts = root / "contracts"
    findings: list[Finding] = []
    if not contracts.is_dir():
        return findings
    for entry in sorted(contracts.iterdir()):
        relative = entry.relative_to(root).as_posix()
        if entry.is_file():
            if entry.name != "README.md":
                findings.append(Finding("CONTRACT_LAYOUT_INVALID", relative, "unexpected file"))
            continue
        if not _NAME.fullmatch(entry.name):
            findings.append(
                Finding("CONTRACT_LAYOUT_INVALID", relative, "name must be lowercase-kebab-case")
            )
        versions = [child for child in entry.iterdir() if child.is_dir()]
        if not versions:
            findings.append(Finding("CONTRACT_LAYOUT_INVALID", relative, "no v<N> directory"))
        for child in sorted(entry.iterdir()):
            if not (child.is_dir() and _VERSION.fullmatch(child.name)):
                findings.append(
                    Finding(
                        "CONTRACT_LAYOUT_INVALID",
                        child.relative_to(root).as_posix(),
                        "only v<N> directories are allowed here",
                    )
                )
    return findings


def check_contract(contract: ContractVersion, root: Path) -> list[Finding]:
    relative = contract.directory.relative_to(root).as_posix()
    findings: list[Finding] = []
    if not contract.document.exists():
        return [Finding("CONTRACT_LAYOUT_INVALID", relative, "no schema.json or openapi.yaml")]
    if (contract.directory / "schema.json").exists() and (
        contract.directory / "openapi.yaml"
    ).exists():
        findings.append(
            Finding("CONTRACT_LAYOUT_INVALID", relative, "both schema.json and openapi.yaml")
        )
    findings += stray_example_files(contract, root)
    if not (contract.directory / "README.md").exists():
        findings.append(Finding("CONTRACT_LAYOUT_INVALID", relative, "README.md is missing"))
    document_path = contract.document.relative_to(root).as_posix()
    try:
        document = load_document(contract)
    except (ValueError, yaml.YAMLError) as error:
        return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, str(error))]

    remote = [
        Finding("CONTRACT_SCHEMA_INVALID", document_path, f"non-local $ref at {reference}")
        for reference in non_local_references(document)
    ]
    remote += [
        Finding("CONTRACT_SCHEMA_INVALID", document_path, f"unresolved local $ref at {reference}")
        for reference in unresolved_references(document)
    ]
    if remote:
        return [*findings, *remote]
    targets: dict[str, str] = {}
    if contract.is_openapi:
        if not str(document.get("openapi", "")).startswith("3.1."):
            return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, "not OpenAPI 3.1")]
        try:
            validate_openapi(document)
        except OpenAPIValidationError as error:
            return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, error.message)]
        schemas = document.get("components", {}).get("schemas", {})
        targets = {name: f"#/components/schemas/{name}" for name in schemas}
        visited: set[str] = set()
        for pointer, schema in openapi_schemas(document):
            findings += [
                Finding("CONTRACT_OPEN_OBJECT", document_path, open_pointer)
                for open_pointer in open_objects(schema, pointer, document, visited)
            ]
    else:
        if document.get("$schema") != JSON_SCHEMA_DIALECT:
            findings.append(
                Finding(
                    "CONTRACT_SCHEMA_INVALID",
                    document_path,
                    f"$schema must be {JSON_SCHEMA_DIALECT}",
                )
            )
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as error:
            return [*findings, Finding("CONTRACT_SCHEMA_INVALID", document_path, error.message)]
        for field in ("$id", "title"):
            if field not in document:
                findings.append(
                    Finding("CONTRACT_SCHEMA_INVALID", document_path, f"{field} missing")
                )
        findings += [
            Finding("CONTRACT_OPEN_OBJECT", document_path, pointer)
            for pointer in dict.fromkeys(open_objects(document))
        ]

    for kind in ("valid", "invalid"):
        examples = sorted((contract.directory / "examples" / kind).glob("*.json"))
        if not examples:
            findings.append(
                Finding("CONTRACT_LAYOUT_INVALID", relative, f"needs at least one {kind} example")
            )
        for example in examples:
            findings += check_example(example, kind, document, targets, root)
    return list(dict.fromkeys(findings))


def stray_example_files(contract: ContractVersion, root: Path) -> list[Finding]:
    """Anything in examples/ besides valid/*.json, invalid/*.json and matching .expected.txt."""
    examples = contract.directory / "examples"
    findings: list[Finding] = []
    if not examples.is_dir():
        return findings
    for path in sorted(examples.rglob("*")):
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(examples).parts
        if path.is_dir():
            if len(parts) != 1 or parts[0] not in {"valid", "invalid"}:
                findings.append(Finding("CONTRACT_LAYOUT_INVALID", relative, "unexpected folder"))
            continue
        allowed = path.suffix == ".json" or (
            parts[0] == "invalid"
            and path.name.endswith(".expected.txt")
            and path.with_name(path.name.removesuffix(".expected.txt") + ".json").exists()
        )
        if len(parts) != 2 or not allowed:
            findings.append(Finding("CONTRACT_LAYOUT_INVALID", relative, "unexpected file"))
    return findings


def check_example(
    example: Path, kind: str, document: dict[str, Any], targets: dict[str, str], root: Path
) -> list[Finding]:
    is_openapi = "openapi" in document
    relative = example.relative_to(root).as_posix()
    schema_name = None
    if is_openapi:
        schema_name = example.name.split(".", 1)[0]
        if schema_name not in targets:
            return [Finding("CONTRACT_LAYOUT_INVALID", relative, f"no schema '{schema_name}'")]
    try:
        instance = json.loads(example.read_text(encoding="utf-8"))
    except ValueError as error:
        return [Finding("CONTRACT_LAYOUT_INVALID", relative, f"not valid JSON: {error}")]
    try:
        errors = sorted(
            error.message
            for error in example_validator(document, schema_name).iter_errors(instance)
        )
    except Unresolvable as error:
        return [Finding("CONTRACT_SCHEMA_INVALID", relative, f"unresolvable $ref: {error}")]
    if kind == "valid":
        return [Finding("CONTRACT_EXAMPLE_REJECTED", relative, "; ".join(errors))] if errors else []
    if not errors:
        return [Finding("CONTRACT_EXAMPLE_ACCEPTED", relative, "the invalid example validates")]
    expected_file = example.with_suffix(".expected.txt")
    expected = expected_file.read_text(encoding="utf-8").strip() if expected_file.exists() else ""
    if not expected:
        return [Finding("CONTRACT_LAYOUT_INVALID", relative, "missing or empty .expected.txt")]
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
    findings = check_layout(root)
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
