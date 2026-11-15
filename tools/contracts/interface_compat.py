"""Interface-contract compatibility with the last release (#486 G3).

    python -m tools.contracts.interface_compat

Compares the record of this checkout with the record committed at the last product release
(the newest `v<semver>` tag HEAD contains): within a major version a contract only grows. These
fail, unless the major version went up: a removed contract (INTERFACE_CONTRACT_REMOVED), a
lowered version (INTERFACE_VERSION_LOWERED), a removed operation (INTERFACE_OPERATION_REMOVED),
a changed signature or capability of an operation (INTERFACE_SIGNATURE_CHANGED), a removed
capability (INTERFACE_CAPABILITY_REMOVED), a removed or renamed error code
(INTERFACE_ERROR_CODE_REMOVED), a removed kit case (INTERFACE_KIT_CASE_REMOVED) or a changed
expectation (INTERFACE_KIT_EXPECTATION_CHANGED). Allowed: new contracts, operations,
capabilities, error codes, kit cases and new parameters with a default. No release tag fails
closed (INTERFACE_RELEASE_UNKNOWN); a release without a record released no interface contract.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from orvex.contracts.interface import Version

from tools.contracts.interface_record import RECORD, current_record
from tools.repository import REPOSITORY_ROOT

# A product tag: `v` and a semantic version, its prerelease after a hyphen (`v0.3.1-alpha.1`).
PRODUCT_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?")


def _operation_problems(
    name: str, operation: str, old: dict[str, Any], new: dict[str, Any]
) -> list[str]:
    where = f"{name}.{operation}"
    problems = []
    if new["capability"] != old["capability"]:
        problems.append(
            f"INTERFACE_SIGNATURE_CHANGED: {where} moved from capability "
            f"{old['capability']} to {new['capability']}"
        )
    if new["returns"] != old["returns"]:
        problems.append(
            f"INTERFACE_SIGNATURE_CHANGED: {where} returns {new['returns']}, was {old['returns']}"
        )
    before, after = old["parameters"], new["parameters"]
    changed = (
        len(after) < len(before)
        or [_shape(p) for p in after[: len(before)]] != [_shape(p) for p in before]
        or any(p["default"] and not q["default"] for p, q in zip(before, after, strict=False))
        or any(not p["default"] for p in after[len(before) :])
    )
    if changed:
        problems.append(f"INTERFACE_SIGNATURE_CHANGED: {where} changed its parameters")
    return problems


def _shape(parameter: dict[str, Any]) -> tuple[str, str, str]:
    return parameter["name"], parameter["kind"], parameter["type"]


def _contract_problems(name: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    problems = []
    for operation, before in sorted(old["operations"].items()):
        after = new["operations"].get(operation)
        if after is None:
            problems.append(f"INTERFACE_OPERATION_REMOVED: {name}.{operation}")
        else:
            problems += _operation_problems(name, operation, before, after)
    problems += [
        f"INTERFACE_CAPABILITY_REMOVED: {name}: {capability}"
        for capability in sorted(set(old["capabilities"]) - set(new["capabilities"]))
    ]
    problems += [
        f"INTERFACE_ERROR_CODE_REMOVED: {name}: {code}"
        for code in sorted(set(old["errors"]) - set(new["errors"]))
    ]
    for case, before in sorted(old["kit"]["cases"].items()):
        after = new["kit"]["cases"].get(case)
        if after is None:
            problems.append(f"INTERFACE_KIT_CASE_REMOVED: {name}: {case}")
        elif after != before:
            problems.append(
                f"INTERFACE_KIT_EXPECTATION_CHANGED: {name}: {case}: {before} became {after}"
            )
    return problems


def incompatibilities(released: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems = []
    for name, old in sorted(released["contracts"].items()):
        new = current["contracts"].get(name)
        if new is None:
            problems.append(f"INTERFACE_CONTRACT_REMOVED: {name}")
            continue
        before, after = Version.parse(old["version"]), Version.parse(new["version"])
        if after < before:
            problems.append(f"INTERFACE_VERSION_LOWERED: {name}: {after}, released {before}")
        elif after.major == before.major:
            problems += _contract_problems(name, old, new)
    return problems


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )


def last_release(root: Path) -> str:
    """The newest product tag (`v<semver>`, docs/process/versioning.md) that HEAD contains."""
    found = _git(
        root,
        "for-each-ref",
        "--merged",
        "HEAD",
        "--sort=-creatordate",
        "--format=%(refname:short)",
        "refs/tags/v*",
    )
    tags = [tag for tag in found.stdout.split() if PRODUCT_TAG.fullmatch(tag)]
    if found.returncode != 0 or not tags:
        raise SystemExit(
            "INTERFACE_RELEASE_UNKNOWN: no product release tag reaches HEAD (fetch the tags) "
            + found.stderr.strip()
        )
    return tags[0]


def released_record(root: Path, tag: str) -> dict[str, Any] | None:
    path = RECORD.relative_to(REPOSITORY_ROOT).as_posix()
    if _git(root, "cat-file", "-e", f"{tag}:{path}").returncode != 0:
        return None
    shown = _git(root, "show", f"{tag}:{path}")
    if shown.returncode != 0:
        raise SystemExit(f"INTERFACE_RELEASE_UNKNOWN: {tag}:{path}: {shown.stderr.strip()}")
    record: dict[str, Any] = json.loads(shown.stdout)
    return record


def run(root: Path) -> int:
    tag = last_release(root)
    released = released_record(root, tag)
    if released is None:
        print(f"interface contracts: release {tag} recorded none; nothing to compare")
        return 0
    problems = incompatibilities(released, current_record())
    for problem in problems:
        print(problem)
    print(f"interface contracts: {len(problems)} incompatibility(ies) with release {tag}")
    return 1 if problems else 0


def main() -> int:
    return run(REPOSITORY_ROOT)


if __name__ == "__main__":
    sys.exit(main())
