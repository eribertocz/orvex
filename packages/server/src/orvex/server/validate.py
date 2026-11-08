"""`validate`: check a project, its semantic model against the lake's current snapshot, and
optionally print the SQL a model runs."""

from __future__ import annotations

from pathlib import Path
from typing import IO

from orvex.server import lake_open, lake_place, project_check, semantic_model
from orvex.server.error_problems import _load_problem, _store_problem
from orvex.server.identity import load_identity
from orvex.server.locking import ProjectLocked, project_lock
from orvex.server.problems import Problem
from orvex.server.project import ProjectLoadError
from orvex.server.project_check import Checked
from orvex.store.errors import StoreError
from orvex.transform.graph import compiled_sql


def _check_semantic(checked: Checked, out: IO[str]) -> bool:
    """The project's semantic model compiled against its lake's current snapshot (#358),
    reported after every transform problem; True when it has problems. Without a lake or a
    snapshot yet, or while a run holds the project, it is not checked, and says so."""
    project = checked.project
    if project is None or checked.place is None or not semantic_model.model_files(project.folder):
        return False
    identity = load_identity()
    home = project.folder / f".{identity.slug}"
    if not lake_place.exists(home):
        print("semantic model not checked: the lake has no snapshot yet", file=out)
        return False
    try:
        with project_lock(home):
            lake = lake_open.open_existing(home, checked.place)
            try:
                loaded = semantic_model.load_semantic(
                    project.folder,
                    lake,
                    lake.current_snapshot().id,
                    (identity.ossie_vendor_name, *identity.previous_vendor_names),
                    connections=project.connections,
                    audience="builder",  # local mode: whoever opens the project builds it
                )
            finally:
                lake.close()
    except ProjectLocked:
        print("semantic model not checked: a run is in progress", file=out)
        return False
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return True
    except StoreError as error:
        print(_store_problem(error, ".", "semantic model"), file=out)
        return True
    for problem in loaded.problems:
        print(problem, file=out)
    return bool(loaded.problems)


def validate(folder: Path, out: IO[str], show_sql: str | None = None) -> int:
    """Check a project; `show_sql`: also print the SQL that runs for that model (views inlined,
    the automatic incremental filter placed)."""
    checked = project_check.check_project(folder)
    for problem in checked.problems:
        print(problem, file=out)
    semantic = _check_semantic(checked, out)
    if checked.problems or semantic:
        return 1
    if show_sql is not None:
        by_name = {model.name: model for model in checked.discovery.models}
        model = by_name.get(show_sql)
        if model is None:
            print(
                Problem("MODEL_REFERENCE_UNKNOWN", ".", None, f"{show_sql} is not a model."),
                file=out,
            )
            return 1
        print(compiled_sql(model, by_name, checked.filtered.get(show_sql)), file=out)
        return 0
    print(f"{len(checked.discovery.models)} models, no problems.", file=out)
    return 0
