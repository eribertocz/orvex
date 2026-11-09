"""The development switches of the Direct Query engines (P2-30 PR 3, #399; P2-31 PR 4, #225;
ADR-0027 §4).

An engine is offered only once the security suite passes on it (P3-13), so until then each
Direct Query engine is enabled only for development, by its environment variable, the slug read
from `identity.toml` (upper case, `-` as `_`): `<SLUG>_DEV_EXISTING_DUCKLAKE` for an existing
DuckLake, `<SLUG>_DEV_CLICKHOUSE` for ClickHouse, set to exactly `1`. Anything else, the
variable absent included, leaves it off: a dataset on that engine's connection is then refused
with `UNSUPPORTED_OPERATION` (`operation: route`). No release build sets either.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from orvex.server.identity import Identity

SUFFIX = "_DEV_EXISTING_DUCKLAKE"
CLICKHOUSE_SUFFIX = "_DEV_CLICKHOUSE"


def variable(identity: Identity, suffix: str = SUFFIX) -> str:
    """A switch's environment variable, derived from the product's slug."""
    return identity.slug.upper().replace("-", "_") + suffix


def existing_ducklake(identity: Identity, environ: Mapping[str, str] = os.environ) -> bool:
    """Whether the existing-DuckLake engine is enabled in this process."""
    return environ.get(variable(identity)) == "1"


def clickhouse(identity: Identity, environ: Mapping[str, str] = os.environ) -> bool:
    """Whether the ClickHouse engine is enabled in this process."""
    return environ.get(variable(identity, CLICKHOUSE_SUFFIX)) == "1"
