"""The threat-model check (#244): `docs/architecture/threat-model.md` lists every HTTP path,
package and contract directory, covers every STRIDE category per surface, and every row names a
rule that exists and a verification of an allowed form. Standard library only; fails closed.

Usage: python3 threat_model.py [repository root]. Exits 1 printing `<CODE> <file>:<line>: <why>`.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

MODEL = "docs/architecture/threat-model.md"
OPENAPI = "contracts/http-api/v1/openapi.yaml"
CATEGORIES = (
    "Spoofing",
    "Tampering",
    "Repudiation",
    "Information disclosure",
    "Denial of service",
    "Elevation of privilege",
)
STALE = "THREAT_MODEL_STALE"
INVALID = "THREAT_MODEL_INVALID"
ROW = re.compile(r"^\| (TM-[A-Z]+-[0-9]+) \|(.*)\|\s*$")
HEADER = re.compile(r"^\| ID \| Category \| Threat \| Rule \| Verification \|\s*$")
SEPARATOR = re.compile(r"^\|(?:-+\|)+\s*$")
SECTION = re.compile(r"^## Surface: (\S+)\s*$")
ADR = re.compile(r"^ADR-([0-9]{4})$")
SECURITY = re.compile(r"^security\.md §([0-9]+(?:\.[0-9]+)*)$")
LAW = re.compile(r"^laws\.md Law ([0-9]+)$")
ROADMAP = re.compile(r"^roadmap:(P[0-9]+-[0-9]+[a-z]?)$")
REASONED = re.compile(r"^(gap|n/a): \S")
# An OpenAPI path key at indent 2. The key ends at its last colon before the end of the line or a
# space, so a colon inside the path (`/x:run`) and an inline value (`/a: {...}`) are both read.
PATH_KEY = re.compile(r"^  (['\"]?)(/\S*?)\1:(?:\s.*)?$")
# The only places a Verification may point to: test files, and security scenario files or folders.
TEST_FILES = re.compile(r"^(?:tests/|packages/[a-z]+/tests/)\S+$")
SCENARIOS = "conformance/security/scenarios/"


@dataclass(frozen=True)
class References:
    """What rows may cite, read once: security.md section numbers, laws and roadmap IDs."""

    security: frozenset[str]
    laws: frozenset[str]
    roadmap: frozenset[str]


def strip_comment(line: str) -> str:
    """A YAML line without its comment (a `#` at the start of the line or after a space)."""
    return re.sub(r"(^|\s)#.*$", "", line).rstrip()


def http_paths(root: Path) -> tuple[set[str], list[str]]:
    """The keys of the OpenAPI document's top-level `paths` mapping, read without a YAML parser,
    and a problem for every indent-2 line inside it that is not a path key (fails closed)."""
    paths: set[str] = set()
    problems: list[str] = []
    inside = found_block = False
    text = (root / OPENAPI).read_text(encoding="utf-8")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = strip_comment(raw)
        if not line:
            continue
        if re.match(r"^\S", line):
            inside = line == "paths:"
            found_block = found_block or inside
            continue
        if not inside or not re.match(r"^  \S", line):
            continue
        if key := PATH_KEY.match(line):
            paths.add(key.group(2))
        else:
            problems.append(f"{INVALID} {OPENAPI}:{number}: {raw.strip()!r} is not a path key")
    if not found_block or not paths:
        problems.append(f"{INVALID} {OPENAPI}:1: no top-level `paths:` mapping with a path")
    return paths, problems


def present(root: Path, paths: set[str]) -> set[tuple[str, str]]:
    """Every item the inventory must list: (kind, name)."""
    items = {("http", path) for path in paths}
    items |= {("package", d.name) for d in (root / "packages").iterdir() if d.is_dir()}
    items |= {
        ("contract", d.name)
        for d in (root / "contracts").iterdir()
        if d.is_dir() and d.name != "_example"
    }
    return items


def references(root: Path) -> References:
    """The headings, laws and roadmap IDs rows may cite, each document read once."""

    def found(path: str, pattern: str) -> frozenset[str]:
        text = (root / path).read_text(encoding="utf-8")
        return frozenset(re.findall(pattern, text, re.MULTILINE))

    return References(
        security=found("docs/architecture/security.md", r"^#+ ([0-9]+(?:\.[0-9]+)*)\.? "),
        laws=found("docs/product/laws.md", r"^### Law ([0-9]+) "),
        roadmap=found("docs/roadmap.md", r"^\| (P[0-9]+-[0-9]+[a-z]?) \|"),
    )


def rule_problem(root: Path, known: References, rule: str) -> str | None:
    if found := SECURITY.match(rule):
        if found.group(1) in known.security:
            return None
        return f"Rule {rule!r} names no security.md heading"
    if found := ADR.match(rule):
        if any((root / "docs/adr").glob(f"{found.group(1)}-*.md")):
            return None
        return f"Rule {rule!r} names no ADR file"
    if found := LAW.match(rule):
        return None if found.group(1) in known.laws else f"Rule {rule!r} names no law"
    return f"Rule {rule!r} is not a security.md section, an ADR or a law"


def verification_problem(root: Path, known: References, value: str, alone: bool) -> str | None:
    if found := ROADMAP.match(value):
        if found.group(1) in known.roadmap:
            return None
        return f"Verification {value!r} names no roadmap ID"
    if REASONED.match(value):
        if value.startswith("n/a") and not alone:
            return f"Verification {value!r}: n/a only on a category with no other row"
        return None
    if value.startswith(("gap:", "n/a:", "roadmap:")):
        return f"Verification {value!r} has no reason or no valid ID"
    target = (root / value).resolve()
    inside = root.resolve() in target.parents
    if inside and TEST_FILES.match(value) and target.is_file():
        return None
    if inside and value.startswith(SCENARIOS) and target.exists():
        return None
    return f"Verification {value!r} is not a test file, a scenario path, a roadmap ID, a gap or n/a"


def check(root: Path) -> list[str]:
    """Every problem of the model at `root`, each `<CODE> <file>:<line>: <why>`."""
    lines = (root / MODEL).read_text(encoding="utf-8").splitlines()
    paths, problems = http_paths(root)
    known = references(root)

    def report(code: str, number: int, why: str) -> None:
        problems.append(f"{code} {MODEL}:{number}: {why}")

    inventory: dict[tuple[str, str], tuple[int, str]] = {}
    sections: dict[str, int] = {}
    rows: dict[str, list[tuple[int, str, str, str, str]]] = {}
    fence = False
    fence_line = 0
    current: str | None = None
    for number, line in enumerate(lines, start=1):
        if line.strip() == "```inventory":
            fence, fence_line = True, number
            continue
        if fence:
            if line.strip() == "```":
                fence = False
                continue
            parts = line.split(maxsplit=2)
            if len(parts) < 3 or parts[0] not in {"http", "package", "contract"}:
                report(INVALID, number, f"inventory line {line!r} is not '<kind> <name> <surface>'")
                continue
            if (parts[0], parts[1]) in inventory:
                report(INVALID, number, f"{parts[0]} {parts[1]} is listed twice in the inventory")
                continue
            inventory[(parts[0], parts[1])] = (number, parts[2])
            continue
        if found := SECTION.match(line):
            current = found.group(1)
            if current in sections:
                report(INVALID, number, f"surface {current} has a second section")
                continue
            sections[current] = number
            rows[current] = []
            continue
        row = ROW.match(line)
        if current is None:
            if row:
                report(INVALID, number, f"row {row.group(1)} is outside a surface section")
            continue
        if not line.startswith("|") or HEADER.match(line) or SEPARATOR.match(line):
            continue
        cells = [cell.strip() for cell in row.group(2).split("|")] if row else []
        if not row or len(cells) != 4:
            report(
                INVALID, number, f"table line {line[:40]!r} is not a 5-column TM-<SURFACE>-<n> row"
            )
            continue
        rows[current].append((number, row.group(1), cells[0], cells[2], cells[3]))
    if fence:
        report(INVALID, len(lines), "the inventory block is not closed")
    if not fence_line:
        report(INVALID, 1, "the model has no inventory block")

    expected = present(root, paths)
    for kind, name in sorted(expected - inventory.keys()):
        report(STALE, fence_line or 1, f"{kind} {name} is missing from the inventory")
    for (kind, name), (number, surface) in sorted(inventory.items(), key=lambda item: item[1][0]):
        if (kind, name) not in expected:
            report(STALE, number, f"{kind} {name} no longer exists")
        if surface.startswith("internal:"):
            if not surface[len("internal:") :].strip():
                report(INVALID, number, f"{kind} {name} is internal with no reason")
        elif surface not in sections:
            report(
                INVALID, number, f"{kind} {name} names surface {surface!r}, which has no section"
            )

    seen: dict[str, int] = {}
    for surface, found_rows in rows.items():
        counts = dict.fromkeys(CATEGORIES, 0)
        for _, _, category, _, _ in found_rows:
            if category in counts:
                counts[category] += 1
        for category in CATEGORIES:
            if not counts[category]:
                report(INVALID, sections[surface], f"surface {surface} has no {category} row")
        for number, row_id, category, rule, value in found_rows:
            if row_id in seen:
                report(INVALID, number, f"{row_id} duplicates the ID of line {seen[row_id]}")
            seen.setdefault(row_id, number)
            if category not in CATEGORIES:
                report(INVALID, number, f"{row_id}: unknown category {category!r}")
            if not rule:
                report(INVALID, number, f"{row_id}: empty Rule")
            elif why := rule_problem(root, known, rule):
                report(INVALID, number, f"{row_id}: {why}")
            if not value:
                report(INVALID, number, f"{row_id}: empty Verification")
            elif why := verification_problem(root, known, value, counts.get(category, 0) == 1):
                report(INVALID, number, f"{row_id}: {why}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    try:
        problems = check(root)
    except (OSError, UnicodeDecodeError) as error:
        print(f"{INVALID} {MODEL}:1: cannot read the repository: {error}")
        return 1
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
