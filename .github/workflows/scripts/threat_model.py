"""The threat-model check (#244): `docs/architecture/threat-model.md` lists every HTTP path,
package and contract directory, covers every STRIDE category per surface, and every row names a
rule that exists and a verification of an allowed form. Standard library only; fails closed.

Usage: python3 threat_model.py [repository root]. Exits 1 printing `<CODE> <file>:<line>: <why>`.
"""

from __future__ import annotations

import re
import sys
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
SECTION = re.compile(r"^## Surface: (\S+)\s*$")
ADR = re.compile(r"^ADR-([0-9]{4})$")
SECURITY = re.compile(r"^security\.md §([0-9]+(?:\.[0-9]+)*)$")
LAW = re.compile(r"^laws\.md Law ([0-9]+)$")
ROADMAP = re.compile(r"^roadmap:(P[0-9]+-[0-9]+[a-z]?)$")
REASONED = re.compile(r"^(gap|n/a): \S")


def http_paths(root: Path) -> set[str]:
    """The keys of the OpenAPI document's top-level `paths` mapping, read without a YAML parser:
    the two-space-indented keys between `paths:` and the next top-level key."""
    paths: set[str] = set()
    inside = False
    for line in (root / OPENAPI).read_text(encoding="utf-8").splitlines():
        if re.match(r"^\S", line):
            inside = line.rstrip() == "paths:"
            continue
        if inside and (found := re.match(r"^  (['\"]?)(/[^'\":]*)\1:\s*$", line)):
            paths.add(found.group(2))
    return paths


def present(root: Path) -> set[tuple[str, str]]:
    """Every item the inventory must list: (kind, name)."""
    items = {("http", path) for path in http_paths(root)}
    items |= {("package", d.name) for d in (root / "packages").iterdir() if d.is_dir()}
    items |= {
        ("contract", d.name)
        for d in (root / "contracts").iterdir()
        if d.is_dir() and d.name != "_example"
    }
    return items


def headings(path: Path, pattern: str) -> set[str]:
    return set(re.findall(pattern, path.read_text(encoding="utf-8"), re.MULTILINE))


def rule_problem(root: Path, rule: str) -> str | None:
    if found := SECURITY.match(rule):
        known = headings(root / "docs/architecture/security.md", r"^#+ ([0-9]+(?:\.[0-9]+)*)\.? ")
        return None if found.group(1) in known else f"Rule {rule!r} names no security.md heading"
    if found := ADR.match(rule):
        exists = any((root / "docs/adr").glob(f"{found.group(1)}-*.md"))
        return None if exists else f"Rule {rule!r} names no ADR file"
    if found := LAW.match(rule):
        known = headings(root / "docs/product/laws.md", r"^### Law ([0-9]+) ")
        return None if found.group(1) in known else f"Rule {rule!r} names no law"
    return f"Rule {rule!r} is not a security.md section, an ADR or a law"


def verification_problem(root: Path, value: str, alone: bool) -> str | None:
    if found := ROADMAP.match(value):
        known = headings(root / "docs/roadmap.md", r"^\| (P[0-9]+-[0-9]+[a-z]?) \|")
        return None if found.group(1) in known else f"Verification {value!r} names no roadmap ID"
    if REASONED.match(value):
        if value.startswith("n/a") and not alone:
            return f"Verification {value!r}: n/a only on a category with no other row"
        return None
    if value.startswith(("gap:", "n/a:", "roadmap:")):
        return f"Verification {value!r} has no reason or no valid ID"
    resolved = (root / value).resolve()
    if root.resolve() in resolved.parents and resolved.exists():
        return None
    return f"Verification {value!r} is not an existing path, a roadmap ID, a gap or n/a"


def check(root: Path) -> list[str]:
    """Every problem of the model at `root`, each `<CODE> <file>:<line>: <why>`."""
    lines = (root / MODEL).read_text(encoding="utf-8").splitlines()
    problems: list[str] = []

    def report(code: str, number: int, why: str) -> None:
        problems.append(f"{code} {MODEL}:{number}: {why}")

    inventory: dict[tuple[str, str], tuple[int, str]] = {}
    sections: dict[str, int] = {}
    rows: dict[str, list[tuple[int, str, str, str, str]]] = {}
    fence = False
    current: str | None = None
    for number, line in enumerate(lines, start=1):
        if line.strip() == "```inventory":
            fence = True
            continue
        if fence:
            if line.strip() == "```":
                fence = False
                continue
            parts = line.split(maxsplit=2)
            if len(parts) < 3 or parts[0] not in {"http", "package", "contract"}:
                report(INVALID, number, f"inventory line {line!r} is not '<kind> <name> <surface>'")
                continue
            inventory[(parts[0], parts[1])] = (number, parts[2])
            continue
        if found := SECTION.match(line):
            current = found.group(1)
            sections[current] = number
            rows[current] = []
            continue
        if found := ROW.match(line):
            cells = [cell.strip() for cell in found.group(2).split("|")]
            if current is None or len(cells) != 4:
                report(
                    INVALID, number, f"row {found.group(1)} is outside a surface or not 5 columns"
                )
                continue
            rows[current].append((number, found.group(1), cells[0], cells[2], cells[3]))
    if fence:
        report(INVALID, len(lines), "the inventory block is not closed")

    for kind, name in sorted(present(root) - inventory.keys()):
        report(STALE, 1, f"{kind} {name} is missing from the inventory")
    for (kind, name), (number, _) in sorted(inventory.items()):
        if (kind, name) not in present(root):
            report(STALE, number, f"{kind} {name} no longer exists")
    for (kind, name), (number, surface) in sorted(inventory.items(), key=lambda i: i[1][0]):
        if not surface.startswith("internal: ") and surface not in sections:
            report(
                INVALID, number, f"{kind} {name} names surface {surface!r}, which has no section"
            )
        if surface.startswith("internal:") and not surface[len("internal:") :].strip():
            report(INVALID, number, f"{kind} {name} is internal with no reason")

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
            elif why := rule_problem(root, rule):
                report(INVALID, number, f"{row_id}: {why}")
            if not value:
                report(INVALID, number, f"{row_id}: empty Verification")
            elif why := verification_problem(root, value, counts.get(category, 0) == 1):
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
