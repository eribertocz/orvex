"""What the architecture checks share: a finding, an invalid allow-list, the product files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    path: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.path}:{self.line}: {self.detail}"


class AllowListError(ValueError):
    code = "MATERIALIZATION_ALLOW_LIST_INVALID"


def product_files(root: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for package in sorted(path for path in (root / "packages").iterdir() if path.is_dir()):
        for path in sorted(package.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if "/src/" in relative:
                found.append((path, relative))
    return found
