"""Select the tests a change affects (#375, #373 §3): conservative, explained, never silent.

    python -m tools.ci.impact                  # against the merge base with origin/main
    python -m tools.ci.impact FILE...          # for the given changed files

A changed file selects:
- **in a package** (`packages/<name>/…`: code, SQL, YAML, JSON, schemas, fixtures, tests): that
  package's tests, and the tests of every package and test folder that depends on it, directly or
  through others (the packages' `pyproject.toml` dependencies);
- **a known narrower path** (`tools/ci/impact.toml`, `[[narrow]]`): the test folders it lists;
- **a global path** (`[full]`: shared code, CI, locks, the public contracts) or **any path the
  map does not know: the full suite.**

The output says what was chosen and why, one reason per changed file.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tools.repository import REPOSITORY_ROOT

MAP = Path(__file__).with_name("impact.toml")


@dataclass
class Selection:
    full: bool = False
    tests: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)

    def paths(self) -> list[str]:
        return ["."] if self.full else sorted(self.tests)


def packages(root: Path) -> dict[str, set[str]]:
    """Each package folder name and the package folders it depends on."""
    names: dict[str, str] = {}
    wanted: dict[str, list[str]] = {}
    for pyproject in sorted((root / "packages").glob("*/pyproject.toml")):
        project = tomllib.loads(pyproject.read_text("utf-8"))["project"]
        folder = pyproject.parent.name
        names[str(project["name"])] = folder
        wanted[folder] = [
            requirement.split(";")[0].split("[")[0].split("=")[0].split(">")[0].split("<")[0]
            for requirement in project.get("dependencies", [])
        ]
    return {
        folder: {names[name.strip()] for name in requirements if name.strip() in names}
        for folder, requirements in wanted.items()
    }


def dependents(graph: Mapping[str, set[str]], changed: Iterable[str]) -> set[str]:
    """`changed` and every package that depends on one of them, at any depth."""
    reached = set(changed)
    grown = True
    while grown:
        grown = False
        for folder, needs in graph.items():
            if folder not in reached and needs & reached:
                reached.add(folder)
                grown = True
    return reached


def select(root: Path, changed: Sequence[str]) -> Selection:
    impact = tomllib.loads(MAP.read_text("utf-8"))
    graph = packages(root)
    folders: Mapping[str, list[str]] = impact["tests"]
    selection = Selection()
    modules: set[str] = set()

    def everything(path: str, why: str) -> None:
        selection.full = True
        selection.reasons.append(f"{path}: {why}: the full suite")

    for path in changed:
        parts = Path(path).parts
        if not parts:
            continue
        if any(path == p.rstrip("/") or path.startswith(p) for p in impact["full"]["paths"]):
            everything(path, "a global path")
            continue
        if parts[0] == "packages" and len(parts) > 1 and parts[1] in graph:
            modules.add(parts[1])
            selection.reasons.append(f"{path}: package {parts[1]}")
            continue
        if parts[0] == "tests" and len(parts) > 1 and f"tests/{parts[1]}" in folders:
            selection.tests.add(f"tests/{parts[1]}")
            selection.reasons.append(f"{path}: its own test folder")
            continue
        narrow = next(
            (
                rule
                for rule in impact.get("narrow", [])
                if any(path == p.rstrip("/") or path.startswith(p) for p in rule["paths"])
            ),
            None,
        )
        if narrow is None:
            everything(path, "not in the impact map")
            continue
        selection.tests |= set(narrow.get("tests", []))
        modules |= set(narrow.get("modules", []))
        selection.reasons.append(f"{path}: {', '.join(narrow['paths'][:2])}…")
    reached = dependents(graph, modules)
    for folder in sorted(reached):
        if (root / "packages" / folder / "tests").is_dir():
            selection.tests.add(f"packages/{folder}/tests")
    for test_folder, exercised in folders.items():
        if reached & set(exercised):
            selection.tests.add(test_folder)
    if modules:
        selection.reasons.append(
            f"packages reached through dependencies: {', '.join(sorted(reached))}"
        )
    return selection


def changed_files(root: Path) -> list[str]:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
        ).stdout

    base = git("merge-base", "HEAD", "origin/main").strip()
    found = git("diff", "--name-only", "--no-renames", base).splitlines()
    found += git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(found))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    changed = arguments or changed_files(REPOSITORY_ROOT)
    selection = select(REPOSITORY_ROOT, changed)
    for reason in selection.reasons:
        print(f"  {reason}")
    print("selected: " + (" ".join(selection.paths()) or "no tests"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
