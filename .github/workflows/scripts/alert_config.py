"""The CI alert listener's configuration (#428): the watched workflows of `ci_alerts.toml`, read
and validated. A malformed configuration fails the listener; it never watches less silently."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIGURATION = Path(__file__).with_name("ci_alerts.toml")
EVENTS = ("push", "schedule", "workflow_dispatch", "workflow_run", "tag")
CADENCES = ("push", "schedule", "workflow_run", "none")
# The API's event of each class: a tag is a push whose ref is the tag.
API_EVENT = {event: ("push" if event == "tag" else event) for event in EVENTS}


class ConfigurationError(Exception):
    """The configuration cannot be used."""


@dataclass(frozen=True, slots=True)
class Watch:
    workflow: str
    events: tuple[str, ...]
    required_jobs: tuple[str, ...]
    cadence: str
    every_hours: int = 0
    tags: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()


def _strings(entry: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    value = entry.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigurationError(f"{where}: `{key}` must be a list of names")
    return tuple(value)


def parse(text: str) -> tuple[Watch, ...]:
    try:
        entries = tomllib.loads(text).get("watch")
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(str(error)) from error
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("no [[watch]] entry")
    watches: list[Watch] = []
    for entry in entries:
        workflow = entry.get("workflow") if isinstance(entry, dict) else None
        if not isinstance(workflow, str) or not workflow.endswith(".yml"):
            raise ConfigurationError(f"an entry has no workflow file: {entry!r}")
        events = _strings(entry, "events", workflow)
        required = _strings(entry, "required_jobs", workflow)
        cadence = entry.get("cadence")
        every = entry.get("every_hours", 0)
        tags = _strings(entry, "tags", workflow)
        triggers = _strings(entry, "triggers", workflow)
        if not events or any(event not in EVENTS for event in events):
            raise ConfigurationError(f"{workflow}: events must be among {EVENTS}")
        if not required:
            raise ConfigurationError(f"{workflow}: no required job")
        if cadence not in CADENCES:
            raise ConfigurationError(f"{workflow}: cadence must be one of {CADENCES}")
        if not isinstance(every, int) or isinstance(every, bool) or every < 0:
            raise ConfigurationError(f"{workflow}: every_hours must be a whole number")
        if cadence == "schedule" and (every <= 0 or "schedule" not in events):
            raise ConfigurationError(f"{workflow}: a schedule cadence needs every_hours")
        if cadence == "push" and "push" not in events:
            raise ConfigurationError(f"{workflow}: a push cadence needs the push event")
        if (cadence == "workflow_run") != bool(triggers) or (
            cadence == "workflow_run" and "workflow_run" not in events
        ):
            raise ConfigurationError(f"{workflow}: a workflow_run cadence needs its triggers")
        if ("tag" in events) != bool(tags):
            raise ConfigurationError(f"{workflow}: the tag event needs its tag patterns")
        if any(w.workflow == workflow for w in watches):
            raise ConfigurationError(f"{workflow} is listed twice")
        watches.append(Watch(workflow, events, required, str(cadence), every, tags, triggers))
    return tuple(watches)


def load(path: Path = CONFIGURATION) -> tuple[Watch, ...]:
    try:
        return parse(path.read_text("utf-8"))
    except OSError as error:
        raise ConfigurationError(f"cannot read {path}: {error}") from error
