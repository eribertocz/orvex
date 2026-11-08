"""A run's OpenLineage events: built from its models' outcomes, written beside the lake, and
sent to the project's collector."""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from orvex.server import run_metadata
from orvex.server.identity import Identity
from orvex.server.problems import Problem
from orvex.server.project import Project, ProjectLoadError, resolve_secret
from orvex.server.project_check import Checked
from orvex.server.run_metadata import _Outcome
from orvex.server.run_reads import _table_columns
from orvex.server.versions import module_versions
from orvex.store.errors import StoreError
from orvex.store.lake import Lake
from orvex.transform import lineage
from orvex.transform.incremental import Columns

LINEAGE_FOLDER = "lineage"
LINEAGE_TIMEOUT_S = 10  # each request
LINEAGE_DEADLINE_S = 30  # all of a run's events together


def _lineage_events(
    project: Project,
    identity: Identity,
    run_id: str,
    outcomes: list[_Outcome],
    checked: Checked,
    lake: Lake,
) -> list[lineage.Event]:
    """OpenLineage events of the models that started (none for those that never did)."""
    key = lineage.project_key(project.settings.name)
    versions = dict(module_versions(identity))
    scope = lineage.Scope(
        producer=f"urn:{identity.slug}:transform:{versions.get('transform', 'unknown')}",
        job_namespace=f"{identity.slug}://{key}",
        lake_namespace=f"ducklake://{key}",
    )
    by_name = {model.name: model for model in checked.discovery.models}
    described: dict[tuple[str, ...], list[str] | None] = {}

    def columns_of(path: list[str]) -> list[str] | None:
        """The columns of a relation the model reads, from the binder (None when unknown)."""
        parts = tuple(path[1:] if len(path) == 3 and path[0] == "lake" else path)
        if parts not in described:
            relation = ".".join('"' + part.replace('"', '""') + '"' for part in parts)
            try:
                described[parts] = [name for name, _ in lake.describe(f"SELECT * FROM {relation}")]
            except StoreError:
                described[parts] = None
        return described[parts]

    # Models rolled back by a later failure end when the run did, not when they finished.
    ended = max(
        (item.finished_at for item in outcomes if item.started), default=run_metadata._now()
    )
    runs: list[lineage.ModelRun] = []
    for item in outcomes:
        if not item.started:
            continue
        # Column lineage pairs the SELECT's items with its own columns, in its order; the
        # schema facet shows the table as stored (columns added later go to its end).
        selected: Columns = checked.columns.get(item.model.name, [])
        output: Columns = selected
        if item.outcome == "success" and item.model.config.materialized != "view":
            # When the table cannot be read, the schema facet keeps the bound columns, if any.
            with contextlib.suppress(StoreError):
                output = _table_columns(lake, item.model) or output
        runs.append(
            lineage.ModelRun(
                model=item.model,
                run_id=item.lineage_run_id,
                started_at=item.started_at,
                finished_at=item.finished_at if item.outcome == "success" else ended,
                outcome="success" if item.outcome == "success" else "failed",
                output=output,
                inputs=lineage.inputs(item.model, by_name, project.sources, scope.lake_namespace),
                column_lineage=lineage.column_lineage(
                    item.model,
                    selected or output,
                    by_name,
                    project.sources,
                    scope.lake_namespace,
                    columns_of,
                ),
                rows_written=item.rows_written if item.outcome == "success" else None,
                error=None if item.error is None else f"{item.error.code}: {item.error.message}",
            )
        )
    return lineage.events(scope, run_id, runs)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None  # the 3xx response is then raised as an HTTPError


def _publish_lineage(
    project: Project, home: Path, run_id: str, events: list[lineage.Event]
) -> Problem | None:
    """Write the run's events to `<home>/lineage/<run_id>.ndjson`, then POST them to the
    project's collector when one is declared; a failure is a warning, never the run's outcome."""
    if not events:
        return None
    folder = home / LINEAGE_FOLDER
    path = folder / f"{run_id}.ndjson"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
    except OSError as error:
        return Problem(
            "LINEAGE_NOT_SENT",
            f"{home.name}/{LINEAGE_FOLDER}",
            None,
            f"the lineage events could not be written ({error.strerror or type(error).__name__}).",
        )
    settings = project.settings.lineage
    if settings is None:
        return None
    headers = {"Content-Type": "application/json"}
    if settings.api_key is not None:
        try:
            headers["Authorization"] = f"Bearer {resolve_secret(settings.api_key)}"
        except ProjectLoadError as error:
            return Problem("LINEAGE_NOT_SENT", "project.yaml", None, error.error.message)
    # Redirects are refused: following one would turn the POST into a GET without the event,
    # and would carry the key to another host.
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if urllib.parse.urlsplit(settings.url).scheme == "http":
        # A collector on this machine is reached directly: a proxy would see the key in clear.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    deadline = time.monotonic() + LINEAGE_DEADLINE_S
    for event in events:
        remaining = deadline - time.monotonic()
        request = urllib.request.Request(
            settings.url,
            data=json.dumps(event).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            if remaining <= 0:
                raise TimeoutError
            with opener.open(request, timeout=min(LINEAGE_TIMEOUT_S, remaining)) as response:
                response.read()
        except Exception as error:  # noqa: BLE001 (any failure is a warning, never the run's)
            if isinstance(error, urllib.error.HTTPError):
                error.close()  # an HTTP error keeps the response open until closed
            # Never the key or the URL: only the kind of failure and the file with the events.
            reason = getattr(error, "code", None) or type(error).__name__
            return Problem(
                "LINEAGE_NOT_SENT",
                f"{home.name}/{LINEAGE_FOLDER}/{path.name}",
                None,
                f"the lineage collector did not accept the events ({reason}); they are kept "
                "in this file.",
            )
    return None
