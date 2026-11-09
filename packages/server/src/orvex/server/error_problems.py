"""The commands' problems from the errors they meet: a model, project-load or store error."""

from __future__ import annotations

from orvex.contracts.model_config.v1 import ModelError
from orvex.server.problems import Problem
from orvex.server.project import ProjectLoadError
from orvex.store.errors import StoreError


def _from_model_error(error: ModelError) -> Problem:
    details = error.details
    return Problem(error.code.root, details.file, details.line, error.message)


def _load_problem(error: ProjectLoadError) -> Problem:
    details = error.error.details
    return Problem(error.code, details.file or ".", details.line, error.error.message)


def _store_message(error: StoreError) -> str:
    detail = error.details.get("duckdb", "")
    return error.message + (f" {detail}" if detail else "")


def _store_problem(error: StoreError, file: str, model: str) -> Problem:
    prefix = f"{model}: " if model else ""
    return Problem(error.code, file, None, prefix + _store_message(error))
