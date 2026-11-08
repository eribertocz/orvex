"""Every contract error code the product raises has a test (#241).

    python -m tools.checks.error_codes

The codes are the `*ErrorCode` enumerations of `contracts/*/v1/schema.json`. A code is used when
a string constant of module code (`packages/*/src`, the generated `contracts` package aside)
is exactly that code. It is tested when it
appears as a whole word in a test file (`packages/*/tests`, `tests/`) or in a conformance case
(`conformance/`). Every used code without a test is reported (`ERROR_CODE_UNTESTED`), and the
check fails. Codes the contracts define but no code raises are listed as a note, not a failure:
a contract may reserve a code before the module that raises it exists.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from tools.repository import REPOSITORY_ROOT

TEST_GLOBS = ("packages/*/tests/**/*.py", "tests/**/*.py", "conformance/**/*")
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".sql", ".md", ".txt"}


def contract_codes(root: Path) -> dict[str, str]:
    """Every contract error code, with the contract that defines it."""
    found: dict[str, str] = {}
    for schema in sorted(root.glob("contracts/*/v1/schema.json")):
        definitions = json.loads(schema.read_text("utf-8")).get("$defs", {})
        for name, definition in definitions.items():
            if name.endswith("ErrorCode") and isinstance(definition.get("enum"), list):
                for code in definition["enum"]:
                    found.setdefault(str(code), schema.parent.parent.name)
    return found


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
    texts = []
    for pattern in TEST_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and path.suffix in TEXT_SUFFIXES:
                texts.append(path.read_text("utf-8", errors="replace"))
    words = set(re.findall(r"[A-Z][A-Z0-9_]+", "\n".join(texts)))
    return set(codes) & words


def problems(root: Path) -> tuple[list[str], list[str]]:
    """The failures and the notes."""
    codes = contract_codes(root)
    used = used_codes(root, codes)
    tested = tested_codes(root, codes)
    failures = [
        f"ERROR_CODE_UNTESTED {code} ({codes[code]}): raised in {', '.join(used[code])}, "
        "but no test names it"
        for code in sorted(set(used) - tested)
    ]
    notes = [
        f"note: {code} ({codes[code]}) is defined but no module code raises it"
        for code in sorted(set(codes) - set(used))
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
