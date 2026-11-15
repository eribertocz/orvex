"""A contract version, `MAJOR.MINOR` (#486 G3): within a major version a contract only grows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .interface_errors import InterfaceContractError

VERSION = re.compile(r"([1-9][0-9]*)\.(0|[1-9][0-9]*)")


@dataclass(frozen=True, slots=True, order=True)
class Version:
    """A contract version: within a major version the contract only grows (append-only)."""

    major: int
    minor: int

    @classmethod
    def parse(cls, text: str) -> Version:
        match = VERSION.fullmatch(text)
        if match is None:
            raise InterfaceContractError(
                "INTERFACE_VERSION_INVALID", f"{text!r} is not a version: expected MAJOR.MINOR"
            )
        return cls(int(match[1]), int(match[2]))

    def provides(self, required: Version) -> bool:
        """An implementation at this version serves a consumer that requires `required`."""
        return self.major == required.major and self.minor >= required.minor

    def previous(self) -> Version | None:
        """The previous minor version of the same major (N-1), if there is one."""
        return Version(self.major, self.minor - 1) if self.minor else None

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"
