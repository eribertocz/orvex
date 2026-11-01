"""Turn business meaning into correct results, or stable errors, with receipts."""

from __future__ import annotations

from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orvex.semantic.errors import SemanticError

__all__ = ["SemanticError"]
__version__ = version("orvex-semantic")


def __getattr__(name: str) -> object:
    # Imported on first use, so the package imports without its dependencies installed.
    if name == "SemanticError":
        from orvex.semantic.errors import SemanticError

        return SemanticError
    raise AttributeError(name)
