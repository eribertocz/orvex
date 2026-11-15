"""Where server mode reads its OIDC configuration (#439): `<SLUG>_OIDC_CONFIG`, or `oidc.toml` in
the instance directory, as the access contract's `OidcSetup`: the document's text, checked when
the instance is opened, and the environment its client secrets are read from.

No file means no provider: every OIDC route answers `OIDC_NOT_CONFIGURED`. A file that cannot be
read or parsed refuses the configuration, naming it, never its content. Client secrets are never
in the file: each provider names the environment variable that holds its secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from orvex.contracts.access.errors import OIDC_NOT_CONFIGURED, AccessError
from orvex.contracts.access.values import OidcSetup
from orvex.server.identity import Identity

FILE = "oidc.toml"
_LARGEST = 256 * 1024


def oidc_configuration(
    identity: Identity, instance: Path, environment: Mapping[str, str]
) -> OidcSetup | None:
    prefix = identity.slug.upper().replace("-", "_")
    path = Path(environment.get(f"{prefix}_OIDC_CONFIG") or instance / FILE)
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > _LARGEST:
            raise OSError("not a small regular file")
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise AccessError(
            OIDC_NOT_CONFIGURED,
            "The sign-in configuration cannot be read.",
            {"setting": "file"},
        ) from error
    return OidcSetup(document, dict(environment))
