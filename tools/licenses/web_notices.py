"""Check that a web build carries the license notices of the code bundled into it (#47).

Vite writes `THIRD_PARTY_LICENSES.md` (`build.license` in `web/vite.config.ts`): one section
`## <package> - <version> (<license>)` per bundled dependency, each with its license text.
Tailwind's generated CSS carries its own preserved `/*! tailwindcss … | MIT License … */` banner.

Usage: python tools/licenses/web_notices.py web/build
Exit code 1 with `WEB_NOTICES_MISSING` lines when a notice is missing. Standard library only, so
it runs on any Python 3 the build machine has.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NOTICES_FILE = "THIRD_PARTY_LICENSES.md"
SECTION = re.compile(r"^## (?P<name>\S+) - (?P<version>\S+) \((?P<license>[^)]+)\)$", re.MULTILINE)
TAILWIND_BANNER = re.compile(r"/\*! tailwindcss v\S+ \| MIT License")


def problems(build: Path) -> list[str]:
    """What is missing from `build`'s notices; empty when they are complete."""
    notices = build / NOTICES_FILE
    if not notices.is_file():
        return [f"{NOTICES_FILE} is missing from {build}"]
    text = notices.read_text(encoding="utf-8")
    sections = list(SECTION.finditer(text))
    found: list[str] = []
    if not sections:
        found.append(f"{NOTICES_FILE} lists no bundled package")
    for index, section in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        body = text[section.end() : end].strip()
        if len(body) < 40:  # a license text, not just a heading or a stray line
            found.append(f"{section['name']} {section['version']} has no license text")
    stylesheets = list(build.rglob("*.css"))
    if stylesheets and not any(
        TAILWIND_BANNER.search(sheet.read_text(encoding="utf-8")) for sheet in stylesheets
    ):
        found.append("the built CSS lost Tailwind's MIT License banner")
    return found


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: python tools/licenses/web_notices.py <web build folder>")
        return 2
    found = problems(Path(arguments[0]))
    for problem in found:
        print(f"WEB_NOTICES_MISSING: {problem}")
    print(f"web notices: {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
