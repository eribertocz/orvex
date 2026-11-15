"""The files of a project that a check reads: `project.yaml`, `connections.yaml` and every file
under `models/`, in a stable order."""

from __future__ import annotations

from pathlib import Path

from orvex.server.project import CONNECTIONS_FILE, PROJECT_FILE

MODELS_FOLDER = "models"


def project_files(folder: Path) -> list[Path]:
    """Each such file of the project in `folder` that exists, the project's two files first."""
    paths = [
        folder / name for name in (PROJECT_FILE, CONNECTIONS_FILE) if (folder / name).is_file()
    ]
    models = folder / MODELS_FOLDER
    if models.is_dir():
        paths += sorted(path for path in models.rglob("*") if path.is_file())
    return paths
