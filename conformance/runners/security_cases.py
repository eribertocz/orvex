"""The normative security scenarios as data (#447): every case of
`conformance/security/scenarios/`, the requests nested in it, how a forbidden value is matched,
and where a case's `pending` may point.

The runner (`security_scenarios`) and the checks of the cases themselves
(`tests/conformance/test_security_scenarios.py`) read the cases through this module only.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from tools.checks import error_codes
from tools.repository import REPOSITORY_ROOT

SECURITY = REPOSITORY_ROOT / "conformance/security"
# How a forbidden value is matched: whole, against tokens split on every other character.
SEPARATOR = re.compile(r"[^A-Za-z0-9_./*-]+")


def files() -> list[Path]:
    return sorted((SECURITY / "scenarios").rglob("*.yaml"))


@cache
def cases() -> dict[str, dict[str, Any]]:
    return {path.stem: yaml.safe_load(path.read_text("utf-8")) for path in files()}


def requests(request: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """`request` and every request nested in it (the steps of a sequence)."""
    yield request
    for step in request.get("steps", []):
        yield from requests(step["request"])


@cache
def pending_issues() -> dict[int, str]:
    """The open issues of the current phase a numeric `pending` may name, with their row."""
    loaded = tomllib.loads((SECURITY / "pending-issues.toml").read_text("utf-8"))
    return {int(number): row for number, row in loaded.get("issues", {}).items()}


def current_phase() -> int:
    return error_codes.current_phase(REPOSITORY_ROOT)


@cache
def roadmap_ids() -> frozenset[str]:
    text = (REPOSITORY_ROOT / "docs/roadmap.md").read_text("utf-8")
    return frozenset(re.findall(r"^\| (P[0-9]+-[0-9]{2}[a-z]?) \|", text, re.MULTILINE))


def phase_of(row: str) -> int:
    """The phase of a roadmap row such as `P6-07`."""
    return int(row[1:].split("-", 1)[0])


def later_row(pending: object) -> str | None:
    """The roadmap row a `pending` excuses a case with, when it is a row of a later phase than
    the current one that the roadmap has; None otherwise (an open issue of this phase, an
    unknown row, a row of this phase or an earlier one)."""
    if not isinstance(pending, str) or pending not in roadmap_ids():
        return None
    return pending if phase_of(pending) > current_phase() else None
