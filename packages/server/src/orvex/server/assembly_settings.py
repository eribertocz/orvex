"""Which implementation an instance chooses for each interface contract (#486 §2.2): the
configuration `assembly.py` reads when several installed modules provide one contract.

`<SLUG>_ASSEMBLY_CONFIG`, or `assembly.toml` in the instance directory:

    [implementations]
    result-cache = "alternatives.result_cache_ipc"   # contract = the module that provides it

No file means no choice. A file that cannot be read, another table or key, a contract name that
is not lowercase-kebab-case or a module that is not a package path is refused
(ASSEMBLY_CONFIG_INVALID), so the server does not start with a choice it misread.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from orvex.contracts.interface import NAME
from orvex.contracts.manifest import MODULE
from orvex.server.assembly import AssemblyRefused
from orvex.server.identity import Identity

FILE = "assembly.toml"
TABLE = "implementations"
_LARGEST = 64 * 1024


def _refused(path: Path, text: str) -> AssemblyRefused:
    return AssemblyRefused([f"ASSEMBLY_CONFIG_INVALID: {path}: {text}"])


def assembly_choices(
    identity: Identity, instance: Path, environment: Mapping[str, str]
) -> dict[str, str]:
    """Contract name to the chosen module, from the instance's configuration."""
    prefix = identity.slug.upper().replace("-", "_")
    path = Path(environment.get(f"{prefix}_ASSEMBLY_CONFIG") or instance / FILE)
    if not path.exists():
        return {}
    try:
        if not path.is_file() or path.stat().st_size > _LARGEST:
            raise OSError("not a small regular file")
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _refused(path, f"cannot be read: {error}") from None
    if set(document) - {TABLE}:
        raise _refused(path, f"only the [{TABLE}] table is read, not {sorted(document)}")
    choices = document.get(TABLE, {})
    if not isinstance(choices, dict):
        raise _refused(path, f"[{TABLE}] must be a table")
    for contract, module in choices.items():
        if not NAME.fullmatch(contract):
            raise _refused(path, f"{contract!r} is not a contract name")
        if not isinstance(module, str) or not MODULE.fullmatch(module):
            raise _refused(path, f"{contract}: {module!r} is not a module path")
    return dict(choices)
