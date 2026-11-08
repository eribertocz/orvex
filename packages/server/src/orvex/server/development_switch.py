"""The development switch of the existing-DuckLake engine (P2-30 PR 3, #399; ADR-0027 §4).

An engine is offered only once the security suite passes on it (P3-13), so until then the
existing-DuckLake engine is enabled only for development: the environment variable
`<SLUG>_DEV_EXISTING_DUCKLAKE`, the slug read from `identity.toml` (upper case, `-` as `_`),
set to exactly `1`. Anything else, the variable absent included, leaves it off: a dataset on an
existing-DuckLake connection is then refused with `UNSUPPORTED_OPERATION` (`operation: route`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from orvex.server.identity import Identity

SUFFIX = "_DEV_EXISTING_DUCKLAKE"


def variable(identity: Identity) -> str:
    """The switch's environment variable, derived from the product's slug."""
    return identity.slug.upper().replace("-", "_") + SUFFIX


def existing_ducklake(identity: Identity, environ: Mapping[str, str] = os.environ) -> bool:
    """Whether the existing-DuckLake engine is enabled in this process."""
    return environ.get(variable(identity)) == "1"
