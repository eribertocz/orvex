"""Product-name check (docs/process/engineering.md §1).

The display name and the slug must come from `identity.toml`. They may appear literally only in
the exempt locations, all of which the rename script rewrites, and as part of a namespace path
(`<namespace>.<module>`), a distribution name (`<slug>-<module>`) or a package directory
(`<namespace>/<module>`).
"""

from __future__ import annotations

import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.repository import MODULES, REPOSITORY_ROOT, identity, read_text, tracked_files

EXEMPT = (
    "*.md",
    "NOTICE",
    "identity.toml",
    "packages/server/src/*/server/identity.toml",
    "packages/*/pyproject.toml",
    ".gitignore",
    ".github/labels.yml",
    ".github/ISSUE_TEMPLATE/*",
    # Test fixtures that assert the identity loader and the rename script.
    "packages/server/tests/test_identity.py",
    "tests/tools/test_checks.py",
    "tests/tools/test_rename.py",
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"PRODUCT_NAME_HARDCODED: {self.path}:{self.line}: {self.text.strip()}"


def is_exempt(relative: str) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in EXEMPT)


def occurrence_pattern(name: str, slug: str) -> re.Pattern[str]:
    words = "|".join(re.escape(word) for word in {name, slug})
    modules = "|".join(MODULES)
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{words})(?![A-Za-z0-9])"
        rf"(?!\.[a-z_]|-(?:{modules})\b|/(?:{modules})\b)",
        re.IGNORECASE,
    )


def scan(root: Path) -> list[Finding]:
    product = identity(root)
    pattern = occurrence_pattern(str(product["name"]), str(product["slug"]))
    findings: list[Finding] = []
    for path in tracked_files(root):
        relative = path.relative_to(root).as_posix()
        if is_exempt(relative):
            continue
        text = read_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                findings.append(Finding(relative, number, line))
    return findings


def main() -> int:
    findings = scan(REPOSITORY_ROOT)
    for finding in findings:
        print(finding)
    print(f"product-name: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
