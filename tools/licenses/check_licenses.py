"""Check every Python and npm dependency against the license allow-list (standard library only).

Usage: python tools/licenses/check_licenses.py [--npm-lock web/package-lock.json]
Exit code 1 and `LICENSE_NOT_ALLOWED` lines when a dependency is not allowed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALLOW_LIST = Path(__file__).with_name("allowed.toml")

# Trove classifiers of older packages without a License-Expression (PEP 639).
CLASSIFIER_TO_SPDX = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


@dataclass(frozen=True, slots=True)
class Dependency:
    ecosystem: str
    name: str
    license: str | None
    runtime: bool


@dataclass(frozen=True, slots=True)
class Policy:
    runtime: frozenset[str]
    development: frozenset[str]
    exceptions: Mapping[str, str]

    def allows(self, dependency: Dependency) -> bool:
        if dependency.name in self.exceptions:
            return True
        if not dependency.license:
            return False
        allowed = self.runtime if dependency.runtime else self.runtime | self.development
        return expression_allowed(dependency.license, allowed)


def load_policy(path: Path = ALLOW_LIST) -> Policy:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    exceptions = data.get("exceptions", {})
    for name, reason in exceptions.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"exception for '{name}' has no reason")
    return Policy(frozenset(data["runtime"]), frozenset(data["development"]), exceptions)


def expression_allowed(expression: str, allowed: frozenset[str]) -> bool:
    """Evaluate a simple SPDX expression: `OR` needs one allowed side, `AND` needs all."""
    cleaned = expression.strip().strip("()")
    alternatives = re.split(r"\s+OR\s+", cleaned)
    return any(
        all(term.strip().strip("()") in allowed for term in re.split(r"\s+AND\s+", alternative))
        for alternative in alternatives
    )


class MetadataFields(Protocol):
    def get(self, name: str, failobj: None = None) -> str | None: ...

    def get_all(self, name: str, failobj: None = None) -> list[str] | None: ...


def python_license(metadata: MetadataFields) -> str | None:
    expression = metadata.get("License-Expression")
    if expression:
        return expression
    for classifier in metadata.get_all("Classifier") or []:
        if classifier in CLASSIFIER_TO_SPDX:
            return CLASSIFIER_TO_SPDX[classifier]
    # Older metadata: a short `License` field that is already an SPDX identifier.
    legacy = (metadata.get("License") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9.+-]+", legacy):
        return legacy
    return None


def python_dependencies(workspace: Iterable[str]) -> list[Dependency]:
    """Installed distributions; runtime = the transitive requirements of the workspace packages."""
    installed = {_normalize(d.metadata["Name"]): d for d in distributions()}
    runtime: set[str] = set()
    pending = [_normalize(name) for name in workspace]
    while pending:
        name = pending.pop()
        distribution = installed.get(name)
        if distribution is None or name in runtime:
            continue
        runtime.add(name)
        for requirement in distribution.requires or []:
            if "extra ==" in requirement:
                continue
            pending.append(_normalize(re.split(r"[\s;<>=!~\[(]", requirement, maxsplit=1)[0]))
    workspace_names = {_normalize(name) for name in workspace}
    return [
        Dependency("python", name, python_license(distribution.metadata), name in runtime)
        for name, distribution in sorted(installed.items())
        if name not in workspace_names
    ]


def npm_dependencies(lock_file: Path) -> list[Dependency]:
    lock = json.loads(lock_file.read_text(encoding="utf-8"))
    found: list[Dependency] = []
    for path, entry in sorted(lock["packages"].items()):
        if not path:
            continue
        license_value = entry.get("license")
        found.append(
            Dependency(
                "npm",
                path.rsplit("node_modules/", 1)[-1],
                license_value if isinstance(license_value, str) else None,
                not entry.get("dev", False),
            )
        )
    return found


def workspace_distributions() -> list[str]:
    names = []
    for project in sorted((REPOSITORY_ROOT / "packages").glob("*/pyproject.toml")):
        names.append(tomllib.loads(project.read_text(encoding="utf-8"))["project"]["name"])
    return names


def violations(dependencies: Iterable[Dependency], policy: Policy) -> list[Dependency]:
    return [dependency for dependency in dependencies if not policy.allows(dependency)]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npm-lock", type=Path, default=REPOSITORY_ROOT / "web/package-lock.json")
    arguments = parser.parse_args()

    policy = load_policy()
    dependencies = python_dependencies(workspace_distributions())
    if arguments.npm_lock.exists():
        dependencies += npm_dependencies(arguments.npm_lock)
    rejected = violations(dependencies, policy)
    for dependency in rejected:
        kind = "runtime" if dependency.runtime else "development"
        print(
            f"LICENSE_NOT_ALLOWED: {dependency.ecosystem} {dependency.name} "
            f"({kind}): {dependency.license or 'no license declared'}"
        )
    print(f"licenses: {len(dependencies)} dependencies checked, {len(rejected)} not allowed")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
