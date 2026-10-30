"""#173: every error code a contract's schema enumerates is documented in that contract's
README error table, so a table cannot fall behind its schema again."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def enum_codes(contract: str) -> set[str]:
    schema = json.loads((ROOT / "contracts" / contract / "v1" / "schema.json").read_text())
    found: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("ErrorCode") and isinstance(value, dict):
                    found.update(str(code) for code in value.get("enum", []))
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(schema)
    return found


def table_codes(contract: str) -> set[str]:
    readme = (ROOT / "contracts" / contract / "v1" / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", readme, re.MULTILINE))


@pytest.mark.parametrize("contract", ["store", "model-config", "project"])
def test_every_enumerated_error_code_is_in_the_readme_table(contract: str) -> None:
    codes = enum_codes(contract)
    assert codes, f"no ErrorCode enum found in {contract}"
    assert codes <= table_codes(contract), sorted(codes - table_codes(contract))
