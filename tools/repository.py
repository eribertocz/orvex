"""Shared helpers for repository tools: which files exist and what the product identity is."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULES = ("store", "transform", "semantic", "access", "dashboards", "mcp", "server", "contracts")
_SKIPPED_DIRECTORIES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".hypothesis",
    ".svelte-kit",
    "build",
    "dist",
}


def tracked_files(root: Path) -> list[Path]:
    """Files under version control (`git ls-files`), or every non-generated file outside Git."""
    if (root / ".git").exists():
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        ).stdout.decode()
        return sorted(
            root / name for name in listing.split("\0") if name and (root / name).is_file()
        )
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(
            part in _SKIPPED_DIRECTORIES or part.endswith(".egg-info")
            for part in path.relative_to(root).parts
        )
    )


def read_text(path: Path) -> str | None:
    """The file's text, or None for binary files."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def identity(root: Path) -> dict[str, Any]:
    product: dict[str, Any] = tomllib.loads((root / "identity.toml").read_text())["product"]
    return product
