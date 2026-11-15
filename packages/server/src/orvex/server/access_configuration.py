"""The access contract's `Configuration` of an instance (#436, #437, #439): its store chosen by
the mode, its audit key and anchor file, its recovery file and, when asked, its OIDC providers,
all from the instance directory and the environment."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from orvex.contracts.access.values import Configuration
from orvex.server.access_store import Mode, access_store
from orvex.server.audit_settings import AuditSettings
from orvex.server.identity import Identity
from orvex.server.oidc_settings import oidc_configuration

RECOVERY_FILE = "access-recovery.key"


def access_configuration(
    mode: Mode,
    identity: Identity,
    instance: Path,
    environment: Mapping[str, str],
    audit: AuditSettings,
    *,
    with_oidc: bool,
) -> Configuration:
    """The instance's configuration; `with_oidc` reads its providers (the sign-in routes do,
    the `admin` commands never reach a provider)."""
    return Configuration(
        store=access_store(mode, identity, instance, environment),
        audit_key=str(audit.key),
        audit_anchors=str(audit.anchors),
        recovery_file=str(instance / RECOVERY_FILE),
        oidc=oidc_configuration(identity, instance, environment) if with_oidc else None,
    )
