"""The contracts every module may import: types generated from `contracts/`, and the format of
interface contracts, their kits and module manifests (ADR-0033).

The generated packages (`<contract>/v<N>.py`) are never edited by hand; the hand-written
modules at this level are listed in docs/process/git-workflow.md (counted in a size).
"""

from importlib.metadata import version

__version__ = version("orvex-contracts")
