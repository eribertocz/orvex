"""Every contract error code the product raises has a test, and every other one is reserved
for a phase (#241, #427).

    python -m tools.checks.error_codes

The codes are every registry the contracts keep: the `*ErrorCode` enumerations of
`contracts/*/v1/schema.json` and the `components.x-error-codes` of `contracts/*/v1/openapi.yaml`.
A code is used when
a string constant of module code (`packages/*/src`, the generated `contracts` package aside) is
exactly that code. It is tested when a string constant of a test module (`packages/*/tests`,
`tests/`) holds it as a whole word, or a conformance case (`conformance/`: YAML, JSON or SQL)
names it; comments and prose do not count. Every used code without a test is reported
(`ERROR_CODE_UNTESTED`). A code no module raises must be listed, with the phase that raises it,
in `tools/checks/reserved-error-codes.toml` (`ERROR_CODE_UNRESERVED`), and a reserved code that
is raised must leave that list (`ERROR_CODE_RESERVED_BUT_RAISED`). Any of them fails the check.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

import yaml

from tools.repository import REPOSITORY_ROOT

RESERVED = "tools/checks/reserved-error-codes.toml"

TEST_GLOBS = ("packages/*/tests/**/*.py", "tests/**/*.py", "conformance/**/*")
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".sql"}


def contract_codes(root: Path) -> dict[str, str]:
    """Every contract error code, with the contract that defines it: each schema's
    `*ErrorCode` enumerations and each OpenAPI contract's `components.x-error-codes`."""
    found: dict[str, str] = {}
    for schema in sorted(root.glob("contracts/*/v1/schema.json")):
        definitions = json.loads(schema.read_text("utf-8")).get("$defs", {})
        for name, definition in definitions.items():
            if name.endswith("ErrorCode") and isinstance(definition.get("enum"), list):
                for code in definition["enum"]:
                    found.setdefault(str(code), schema.parent.parent.name)
    for api in sorted(root.glob("contracts/*/v1/openapi.yaml")):
        document = yaml.safe_load(api.read_text("utf-8")) or {}
        registry = (document.get("components") or {}).get("x-error-codes") or {}
        for code in registry:
            found.setdefault(str(code), api.parent.parent.name)
    return found


def reserved_codes(root: Path) -> dict[str, str]:
    """Each reserved code with the phase that raises it."""
    path = root / RESERVED
    if not path.is_file():
        return {}
    listed = tomllib.loads(path.read_text("utf-8")).get("reserved", {})
    return {str(code): str(entry["phase"]) for code, entry in listed.items()}


def _constants(path: Path) -> Iterable[str]:
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def used_codes(root: Path, codes: Iterable[str]) -> dict[str, list[str]]:
    """Each code module code raises, with the files that name it."""
    wanted = set(codes)
    found: dict[str, list[str]] = {}
    for path in sorted(root.glob("packages/*/src/**/*.py")):
        if path.relative_to(root).parts[1] == "contracts":
            continue  # generated from the schemas: it lists every code, raising none
        for value in set(_constants(path)) & wanted:
            found.setdefault(value, []).append(path.relative_to(root).as_posix())
    return found


def tested_codes(root: Path, codes: Iterable[str]) -> set[str]:
    """The codes a test asserts: a string constant of a test module that holds the code as a
    whole word, or a word of a conformance case (YAML, JSON or SQL data, never prose)."""
    words: set[str] = set()
    for pattern in TEST_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if path.suffix == ".py":
                texts: Iterable[str] = _constants(path)
            else:
                texts = [path.read_text("utf-8", errors="replace")]
            for text in texts:
                words.update(re.findall(r"[A-Z][A-Z0-9_]+", text))
    return set(codes) & words


def problems(root: Path) -> tuple[list[str], list[str]]:
    """The failures and the notes."""
    codes = contract_codes(root)
    used = used_codes(root, codes)
    tested = tested_codes(root, codes)
    reserved = reserved_codes(root)
    failures = [
        f"ERROR_CODE_UNTESTED {code} ({codes[code]}): raised in {', '.join(used[code])}, "
        "but no test names it"
        for code in sorted(set(used) - tested)
    ]
    failures += [
        f"ERROR_CODE_UNRESERVED {code} ({codes[code]}): no module raises it and it is not "
        f"reserved in {RESERVED} with the phase that raises it"
        for code in sorted(set(codes) - set(used) - set(reserved))
    ]
    failures += [
        f"ERROR_CODE_RESERVED_BUT_RAISED {code}: raised in {', '.join(used[code])}; "
        f"remove it from {RESERVED}"
        for code in sorted(set(reserved) & set(used))
    ]
    failures += [
        f"ERROR_CODE_RESERVED_UNKNOWN {code}: no contract defines it; remove it from {RESERVED}"
        for code in sorted(set(reserved) - set(codes))
    ]
    notes = [
        f"note: {code} ({codes[code]}) is reserved for {reserved[code]}"
        for code in sorted(set(codes) & set(reserved))
    ]
    return failures, notes


def main() -> int:
    failures, notes = problems(REPOSITORY_ROOT)
    for line in (*notes, *failures):
        print(line)
    codes = contract_codes(REPOSITORY_ROOT)
    used = len(used_codes(REPOSITORY_ROOT, codes))
    print(f"error codes: {used} raised, {used - len(failures)} tested, {len(codes)} defined")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
