"""OIDC providers and the group mapping, as the administrator configures them (domain, #439):
parsed from the instance's configuration document and validated before any of it is used.

Every refusal is `OIDC_NOT_CONFIGURED`, naming the setting (`provider.<name>.<setting>` or
`mapping.<index>.<setting>`), never a secret.

- `issuer`: an `https` URL, kept exactly as written (no trailing-slash repair).
- `client_id`; the client secret is read from the environment variable the configuration names,
  never from the document itself.
- `algorithms`: an allow-list from `RS256`, `PS256`, `ES256` and `EdDSA`; `none` and `HS*` are
  refused, whatever else is listed.
- `scopes`: added to `openid`.
- `group_refresh_minutes`: how long provider groups are trusted, 5 to 1,440 (24 hours), 60 by
  default.
- `internal`, with `internal_ranges`: a provider on private addresses, reachable only inside
  the listed networks (loopback and link-local stay refused, `provider_fetch.py`).
- `endpoint_hosts`: hosts other than the issuer's that its token and key endpoints may use.
- A mapping entry maps (provider, claim, value) to (project, group). It never grants a role.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from urllib.parse import urlsplit

from orvex.access.directory.names import checked_group, checked_id
from orvex.access.errors import OIDC_NOT_CONFIGURED, AccessError

ALLOWED_ALGORITHMS = ("RS256", "PS256", "ES256", "EdDSA")
REFRESH_RANGE = (timedelta(minutes=5), timedelta(hours=24))
DEFAULT_REFRESH = timedelta(hours=1)
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_CLAIM = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,127}")
_VARIABLE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,64}")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
_PROVIDER_KEYS = frozenset(
    {
        "issuer",
        "client_id",
        "client_secret_variable",
        "algorithms",
        "scopes",
        "group_refresh_minutes",
        "internal",
        "internal_ranges",
        "endpoint_hosts",
    }
)
_MAPPING_KEYS = frozenset({"provider", "claim", "value", "project", "group"})

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    issuer: str
    client_id: str
    client_secret: str
    algorithms: tuple[str, ...]
    scopes: tuple[str, ...]
    group_refresh: timedelta
    internal: bool
    internal_ranges: tuple[Network, ...]
    endpoint_hosts: tuple[str, ...]

    @property
    def issuer_host(self) -> str:
        return (urlsplit(self.issuer).hostname or "").lower()

    def __repr__(self) -> str:  # the secret never reaches a log through a repr
        return f"Provider(name={self.name!r}, issuer={self.issuer!r})"


@dataclass(frozen=True, slots=True)
class MappingRule:
    provider: str
    claim: str
    value: str
    project: str
    group: str


@dataclass(frozen=True, slots=True)
class Configuration:
    providers: Mapping[str, Provider]
    mapping: tuple[MappingRule, ...]

    def provider(self, name: str) -> Provider:
        found = self.providers.get(name)
        if found is None:
            raise AccessError(OIDC_NOT_CONFIGURED, "No such sign-in provider.")
        return found

    def rules_for(self, provider: str) -> tuple[MappingRule, ...]:
        return tuple(rule for rule in self.mapping if rule.provider == provider)


def _refused(setting: str) -> AccessError:
    return AccessError(
        OIDC_NOT_CONFIGURED, f"The sign-in setting {setting} is not valid.", {"setting": setting}
    )


def _text(table: Mapping[str, object], key: str, setting: str, pattern: re.Pattern[str]) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _refused(setting)
    return value


def _texts(table: Mapping[str, object], key: str, setting: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _refused(setting)
    return cast(list[str], value)


def checked_issuer(issuer: object, setting: str) -> str:
    """An `https` URL with a host and no query, fragment or credentials, kept exactly."""
    if not isinstance(issuer, str) or len(issuer) > 512 or not issuer.isascii():
        raise _refused(setting)
    try:
        parts = urlsplit(issuer)
        parts.port  # noqa: B018 (raises on a bad port)
    except ValueError as error:
        raise _refused(setting) from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in issuer)
    ):
        raise _refused(setting)
    return issuer


def _algorithms(table: Mapping[str, object], setting: str) -> tuple[str, ...]:
    listed = _texts(table, "algorithms", setting)
    if not listed or any(item not in ALLOWED_ALGORITHMS for item in listed):
        raise _refused(setting)  # `none`, `HS256` or anything unknown refuses the provider
    return tuple(dict.fromkeys(listed))


def _refresh(table: Mapping[str, object], setting: str) -> timedelta:
    minutes = table.get("group_refresh_minutes", int(DEFAULT_REFRESH.total_seconds() // 60))
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise _refused(setting)
    low, high = REFRESH_RANGE
    found = timedelta(minutes=minutes)
    if not low <= found <= high:
        raise _refused(setting)
    return found


def _ranges(table: Mapping[str, object], internal: bool, setting: str) -> tuple[Network, ...]:
    listed = _texts(table, "internal_ranges", setting)
    if listed and not internal:
        raise _refused(setting)
    try:
        found = tuple(ipaddress.ip_network(item, strict=True) for item in listed)
    except ValueError as error:
        raise _refused(setting) from error
    if internal and not found:
        raise _refused(setting)
    if any(network.is_loopback or network.is_link_local for network in found):
        raise _refused(setting)
    return found


def _provider(name: str, table: object, environment: Mapping[str, str]) -> Provider:
    prefix = f"provider.{name}"
    if not _NAME.fullmatch(name):
        raise _refused(prefix)
    if not isinstance(table, Mapping):
        raise _refused(prefix)
    unknown = sorted(set(table) - _PROVIDER_KEYS)
    if unknown:
        raise _refused(f"{prefix}.{unknown[0]}")
    issuer = checked_issuer(table.get("issuer"), f"{prefix}.issuer")
    client_id = table.get("client_id")
    if (
        not isinstance(client_id, str)
        or not 1 <= len(client_id) <= 256
        or not client_id.isprintable()
    ):
        raise _refused(f"{prefix}.client_id")
    variable = _text(table, "client_secret_variable", f"{prefix}.client_secret_variable", _VARIABLE)
    secret = environment.get(variable)
    if not secret:
        raise _refused(f"{prefix}.client_secret_variable")
    internal = table.get("internal", False)
    if not isinstance(internal, bool):
        raise _refused(f"{prefix}.internal")
    scopes = _texts(table, "scopes", f"{prefix}.scopes")
    if any(not _SCOPE.fullmatch(scope) or scope == "openid" for scope in scopes):
        raise _refused(f"{prefix}.scopes")
    hosts = _texts(table, "endpoint_hosts", f"{prefix}.endpoint_hosts")
    if any(not _HOST.fullmatch(host) for host in hosts):
        raise _refused(f"{prefix}.endpoint_hosts")
    return Provider(
        name=name,
        issuer=issuer,
        client_id=client_id,
        client_secret=secret,
        algorithms=_algorithms(table, f"{prefix}.algorithms"),
        scopes=tuple(dict.fromkeys(scopes)),
        group_refresh=_refresh(table, f"{prefix}.group_refresh_minutes"),
        internal=internal,
        internal_ranges=_ranges(table, internal, f"{prefix}.internal_ranges"),
        endpoint_hosts=tuple(dict.fromkeys(hosts)),
    )


def _rule(index: int, table: object, providers: Mapping[str, Provider]) -> MappingRule:
    prefix = f"mapping.{index}"
    if not isinstance(table, Mapping) or set(table) != _MAPPING_KEYS:
        raise _refused(prefix)
    provider = table["provider"]
    if not isinstance(provider, str) or provider not in providers:
        raise _refused(f"{prefix}.provider")
    claim = _text(table, "claim", f"{prefix}.claim", _CLAIM)
    value = table["value"]
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise _refused(f"{prefix}.value")
    try:
        project = checked_id(table["project"], "project")
        group = checked_group(table["group"])
    except (AccessError, TypeError) as error:
        raise _refused(f"{prefix}.project_or_group") from error
    return MappingRule(provider, claim, value, project, group)


def parsed(document: Mapping[str, object], environment: Mapping[str, str]) -> Configuration:
    """The configuration a document describes, every setting checked."""
    unknown = sorted(set(document) - {"providers", "mapping"})
    if unknown:
        raise _refused(unknown[0])
    tables = document.get("providers", {})
    if not isinstance(tables, Mapping):
        raise _refused("providers")
    providers = {name: _provider(str(name), table, environment) for name, table in tables.items()}
    issuers = [provider.issuer for provider in providers.values()]
    for name, provider in providers.items():
        if issuers.count(provider.issuer) > 1:  # one issuer is one provider: its links are its own
            raise _refused(f"provider.{name}.issuer")
    rules = document.get("mapping", [])
    if not isinstance(rules, Sequence) or isinstance(rules, str):
        raise _refused("mapping")
    mapping = tuple(_rule(index, table, providers) for index, table in enumerate(rules))
    return Configuration(providers, tuple(dict.fromkeys(mapping)))
