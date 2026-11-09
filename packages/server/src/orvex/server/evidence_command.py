"""`evidence`: the builder checks the semantic model's evidence on the lake, on demand and
after each successful run (#355)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import IO

from orvex.semantic.ossie import sql
from orvex.server import evidence_runs, lake_open, lake_place, semantic_model
from orvex.server.error_problems import _load_problem, _store_problem
from orvex.server.identity import Identity
from orvex.server.lake_open import lake_location
from orvex.server.locking import ProjectLocked, project_lock
from orvex.server.problems import Problem
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.store.errors import StoreError
from orvex.store.lake import Lake


def _evidence_after_run(
    project: Project,
    identity: Identity,
    lake: Lake,
    snapshot: int,
    written: Sequence[str],
    run_id: str,
) -> list[str]:
    """The evidence of every dataset a successful run wrote, and of the relationships touching
    one, checked on the snapshot the run committed and recorded (#355). The run stays as it
    went: a semantic model with problems, or a check that cannot run, is reported only."""
    if not written or not semantic_model.model_files(project.folder):
        return []
    loaded = semantic_model.load_semantic(
        project.folder,
        lake,
        snapshot,
        (identity.ossie_vendor_name, *identity.previous_vendor_names),
        connections=project.connections,
        audience="builder",  # local mode: whoever opens the project builds it
    )
    if loaded.model is None:
        return ["evidence not checked: the semantic model has problems", *map(str, loaded.problems)]
    datasets, _ = evidence_runs.touching(loaded.model, written)
    if not datasets:
        return []
    return evidence_runs.verify_and_store(loaded.model, lake, datasets=datasets, run=run_id).lines()


def evidence(folder: Path, identity: Identity, out: IO[str], dataset: str | None = None) -> int:
    """`<slug> evidence`: the project's builder checks the evidence of one dataset, or of every
    one, on the lake's current snapshot (#355). In local mode, whoever opens the project is its
    builder (Owner decision on #358); this grant is the CLI's, never the HTTP API's."""
    try:
        project = load_project(folder)
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    home = project.folder / f".{identity.slug}"
    if not lake_place.exists(home):
        print("evidence not checked: the lake has no snapshot yet", file=out)
        return 1
    try:
        with project_lock(home):
            lake = lake_open.open_existing(home, lake_location(project, home))
            try:
                loaded = semantic_model.load_semantic(
                    project.folder,
                    lake,
                    lake.current_snapshot().id,
                    (identity.ossie_vendor_name, *identity.previous_vendor_names),
                    connections=project.connections,
                    audience="builder",
                )
                if loaded.model is None:
                    for problem in loaded.problems or (
                        Problem("OSSIE_INVALID", f"{semantic_model.FOLDER}/", None, "no model"),
                    ):
                        print(problem, file=out)
                    return 1
                chosen = None
                if dataset is not None:
                    document = loaded.model.compiled.ossie.document
                    names = {
                        sql.normalize(str(d["name"]))
                        for d in evidence_runs.items(document, "datasets")
                    }
                    if sql.normalize(dataset) not in names:
                        print(f"OSSIE_INVALID {loaded.model.file} no dataset {dataset!r}", file=out)
                        return 1
                    chosen = {sql.normalize(dataset)}
                outcome = evidence_runs.verify_and_store(loaded.model, lake, datasets=chosen)
            finally:
                lake.close()
    except ProjectLocked:
        print("evidence not checked: a run is in progress", file=out)
        return 1
    except StoreError as error:
        print(_store_problem(error, ".", "evidence"), file=out)
        return 1
    for line in outcome.lines():
        print(line, file=out)
    return 1 if outcome.problems else 0
