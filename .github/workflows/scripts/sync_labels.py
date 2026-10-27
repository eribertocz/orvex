"""Make the repository's labels exactly equal to `.github/labels.yml`."""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from github_api import GitHubApiError, GitHubClient

_LABEL_LINE = re.compile(
    r'^- \{ name: "(?P<name>[^"]+)",\s*color: "(?P<color>[0-9a-fA-F]{6})",'
    r'\s*description: "(?P<description>[^"]*)" \}$'
)


class LabelFileError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    color: str
    description: str


@dataclass(frozen=True, slots=True)
class SyncPlan:
    create: tuple[Label, ...]
    update: tuple[Label, ...]
    delete: tuple[str, ...]


def parse_label_file(text: str) -> list[Label]:
    """Parse the one-label-per-line format of `.github/labels.yml`; unknown lines are errors."""
    labels: list[Label] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LABEL_LINE.match(line)
        if match is None:
            raise LabelFileError(f"line {number} is not a label definition: {raw!r}")
        labels.append(Label(match["name"], match["color"].lower(), match["description"]))
    names = [label.name for label in labels]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise LabelFileError(f"duplicate labels: {', '.join(duplicates)}")
    return labels


def plan_sync(existing: list[Label], desired: list[Label]) -> SyncPlan:
    current = {label.name: label for label in existing}
    wanted = {label.name: label for label in desired}
    create = tuple(label for name, label in wanted.items() if name not in current)
    update = tuple(
        label for name, label in wanted.items() if name in current and current[name] != label
    )
    delete = tuple(sorted(name for name in current if name not in wanted))
    return SyncPlan(create, update, delete)


def main() -> int:
    client = GitHubClient(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    try:
        desired = parse_label_file(Path(".github/labels.yml").read_text(encoding="utf-8"))
        existing = [
            Label(item["name"], item["color"].lower(), item.get("description") or "")
            for item in client.paginate("/labels")
        ]
        plan = plan_sync(existing, desired)
        for label in plan.create:
            client.request("POST", "/labels", _payload(label))
            print(f"created {label.name}")
        for label in plan.update:
            client.request("PATCH", f"/labels/{_quote(label.name)}", _payload(label))
            print(f"updated {label.name}")
        for name in plan.delete:
            client.request("DELETE", f"/labels/{_quote(name)}")
            print(f"deleted {name}")
    except (LabelFileError, GitHubApiError) as error:
        print(f"::error::{error}")
        return 1
    return 0


def _payload(label: Label) -> dict[str, str]:
    return {"name": label.name, "color": label.color, "description": label.description}


def _quote(name: str) -> str:
    return urllib.parse.quote(name, safe="")


if __name__ == "__main__":
    sys.exit(main())
