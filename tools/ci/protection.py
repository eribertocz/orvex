"""Compare the required checks in `tools/ci/checks.toml` with `main`'s effective protection (#267).

    python -m tools.ci.protection

`main` is protected by two independent mechanisms, and both are read:

- **rulesets:** `GET /repos/{repo}/rules/branches/main` lists the rules that apply to the
  branch, each with its ruleset; every ruleset with a `required_status_checks` rule is read
  again (`GET /repos/{repo}/rulesets/{id}`) and counts only while its enforcement is `active`;
- **classic branch protection:** `GET /repos/{repo}/branches/main` reports it under
  `protection` (readable with a read-only token, unlike the admin-only protection endpoint).

The union of their required contexts must equal the contexts the list marks required. Any
source that cannot be read fails the comparison (fail closed). Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

API = "https://api.github.com/"
LIST = Path(__file__).resolve().parent / "checks.toml"
BRANCH = "main"

# A parsed JSON response: an object or a list.
type Fetch = Callable[[str], Any]


class ProtectionError(Exception):
    """A source of `main`'s protection could not be read."""


def listed_required(path: Path = LIST) -> set[str]:
    jobs = tomllib.loads(path.read_text(encoding="utf-8")).get("job", [])
    return {context for job in jobs if job.get("required") for context in job.get("contexts", [])}


def effective_required(repository: str, fetch: Fetch) -> set[str]:
    """Every status-check context `main` requires, from its rulesets and classic protection."""
    contexts: set[str] = set()
    branch = fetch(f"repos/{repository}/branches/{BRANCH}")
    protection = branch.get("protection")
    if branch.get("protected") and not isinstance(protection, dict):
        raise ProtectionError("the branch is protected but its protection is not reported")
    protection = protection or {}
    if protection.get("enabled"):
        if not isinstance(protection.get("required_status_checks"), dict):
            raise ProtectionError("classic protection is on but its checks are not reported")
        checks = protection.get("required_status_checks") or {}
        if checks.get("enforcement_level", "off") != "off":
            contexts |= set(checks.get("contexts") or [])
            contexts |= {c["context"] for c in checks.get("checks") or []}
    rules = fetch(f"repos/{repository}/rules/branches/{BRANCH}?per_page=100")
    if not isinstance(rules, list):
        raise ProtectionError("the branch rules are not a list")
    if len(rules) >= 100:
        raise ProtectionError("100 or more branch rules: read the next page before comparing")
    rulesets = {r["ruleset_id"] for r in rules if r.get("type") == "required_status_checks"}
    for ruleset_id in sorted(rulesets):
        ruleset = fetch(f"repos/{repository}/rulesets/{ruleset_id}")
        if ruleset.get("enforcement") != "active":
            continue
        for rule in ruleset.get("rules") or []:
            if rule.get("type") == "required_status_checks":
                required = (rule.get("parameters") or {}).get("required_status_checks") or []
                contexts |= {check["context"] for check in required}
    return contexts


def differences(listed: set[str], effective: set[str]) -> list[str]:
    found = [
        f"main requires {c}, which checks.toml does not mark required"
        for c in sorted(effective - listed)
    ]
    found += [
        f"checks.toml marks {c} required, which main does not require"
        for c in sorted(listed - effective)
    ]
    return found


def github_fetch(token: str) -> Fetch:
    def fetch(path: str) -> Any:  # noqa: ANN401 - parsed JSON
        request = urllib.request.Request(
            API + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
            raise ProtectionError(f"{path}: {error}") from None

    return fetch


def main() -> int:
    token = os.environ.get("GH_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        print("::error title=protection::GH_TOKEN and GITHUB_REPOSITORY are required")
        return 1
    try:
        effective = effective_required(repository, github_fetch(token))
    except (ProtectionError, KeyError, TypeError, AttributeError) as error:
        print(f"::error title=protection::cannot read main's protection: {error}")
        return 1
    found = differences(listed_required(), effective)
    for line in found:
        print(f"::error title=protection::{line}")
    if not found:
        print(f"protection: the {len(effective)} required checks of main match checks.toml")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
