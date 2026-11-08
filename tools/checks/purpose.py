"""Every hand-written module states its one purpose (#246; Owner's rule, 2026-11-08).

    python -m tools.checks.purpose

FILE_PURPOSE_MISSING (a failure): a Python module of a package (`packages/*/src`), of `tools/`
or of `conformance/runners/` without a module docstring whose first line says what the file is
for. Generated contract modules are exempt (`structure.generated`), and so is an empty
`__init__.py`, which only marks a package. Whether a file keeps to that one purpose is the
Reviewer's call (`docs/process/review.md`); this check makes the purpose explicit, so a file that
grows a second one is visible against its own first line.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tools.checks.structure import generated
from tools.repository import REPOSITORY_ROOT

ROOTS = ("packages/*/src/**/*.py", "tools/**/*.py", "conformance/runners/**/*.py")


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    text: str

    def __str__(self) -> str:
        return f"error: FILE_PURPOSE_MISSING {self.path}: {self.text}"


def _modules(root: Path) -> Iterator[Path]:
    seen: set[Path] = set()
    for pattern in ROOTS:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root).as_posix()
            if path in seen or generated(relative) or "/tests/" in f"/{relative}":
                continue
            seen.add(path)
            yield path


def findings(root: Path) -> list[Finding]:
    found = []
    for path in _modules(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text("utf-8")
        if path.name == "__init__.py" and not text.strip():
            continue
        try:
            docstring = ast.get_docstring(ast.parse(text, filename=relative))
        except SyntaxError as error:
            found.append(Finding(relative, f"cannot be parsed: {error.msg}"))
            continue
        first = (docstring or "").strip().splitlines()[0].strip() if docstring else ""
        if not first:
            found.append(Finding(relative, "no module docstring stating the file's one purpose"))
    return found


def main(root: Path = REPOSITORY_ROOT) -> int:
    found = findings(root)
    for finding in found:
        print(f"::error file={finding.path}::FILE_PURPOSE_MISSING {finding.text}")
        print(finding)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
