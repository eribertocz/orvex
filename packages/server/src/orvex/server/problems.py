"""A problem the CLI reports: a stable code, where it is, and what is wrong."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Problem:
    code: str
    file: str
    line: int | None
    message: str

    def __str__(self) -> str:
        where = self.file if self.line is None else f"{self.file}:{self.line}"
        return f"{self.code} {where} {self.message}"
