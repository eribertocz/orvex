"""Turn pytest JUnit failures into GitHub Actions error annotations.

    python -m tools.ci.junit_annotations junit.xml

Annotations are readable through the checks API, so a failure can be diagnosed without the raw
job log. Each message is cut to keep annotations small.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

LIMIT = 4000


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def failure_annotations(report: Path) -> list[str]:
    lines = []
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        for problem in [*case.findall("failure"), *case.findall("error")]:
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (problem.get("message") or "") + "\n" + (problem.text or "")
            lines.append(f"::error title={escape(name)}::{escape(detail[-LIMIT:])}")
    return lines


def main() -> int:
    report = Path(sys.argv[1])
    if report.is_file():
        for line in failure_annotations(report):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
