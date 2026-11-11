"""Where server mode reads its OIDC configuration (#439): `<SLUG>_OIDC_CONFIG`, or `oidc.toml` in
the instance directory, checked by `orvex.access.oidc.configuration`.

No file means no provider: every OIDC route answers `OIDC_NOT_CONFIGURED`. A file that cannot be
read or parsed refuses the configuration, naming it, never its content. Client secrets are never
in the file: each provider names the environment variable that holds its secret.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from orvex.access.errors import OIDC_NOT_CONFIGURED, AccessError
from orvex.access.oidc.configuration import Configuration, parsed
from orvex.server.identity import Identity

FILE = "oidc.toml"
_LARGEST = 256 * 1024


def oidc_configuration(
    identity: Identity, instance: Path, environment: Mapping[str, str]
) -> Configuration:
    prefix = identity.slug.upper().replace("-", "_")
    path = Path(environment.get(f"{prefix}_OIDC_CONFIG") or instance / FILE)
    if not path.exists():
        return Configuration({}, ())
    try:
        if not path.is_file() or path.stat().st_size > _LARGEST:
            raise OSError("not a small regular file")
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise AccessError(
            OIDC_NOT_CONFIGURED,
            "The sign-in configuration cannot be read.",
            {"setting": "file"},
        ) from error
    return parsed(document, environment)
