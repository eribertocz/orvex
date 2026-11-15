"""Opening one access instance for the `api` package: its store, its audit log and the services
over them, composed from a contract `Configuration`.

The store is migrated and the audit log opened (its key created the first time only,
`audit/opening.py`). An OIDC configuration is parsed and every provider's discovery document
read at once (`OidcSignIn.start_up`): a configuration the implementation cannot serve refuses
the opening (`OIDC_NOT_CONFIGURED`), and so does one given no provider fetcher. Whatever fails,
the store opened for it is closed.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orvex.access.audit.log import AuditLog
from orvex.access.audit.opening import open_audit_log
from orvex.access.authentication.query_limits import QueryLimits
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.directory.attributes import Attributes
from orvex.access.directory.bootstrap import Bootstrap
from orvex.access.directory.reads import DirectoryReads
from orvex.access.directory.service import Directory
from orvex.access.oidc import configuration as oidc_configuration
from orvex.access.oidc.configuration import Provider
from orvex.access.oidc.identities import Identities
from orvex.access.oidc.service import OidcSignIn
from orvex.access.storage import PostgresStore, SqliteStore, read_settings
from orvex.access.storage.store import Store
from orvex.contracts.access.errors import OIDC_NOT_CONFIGURED, AccessError
from orvex.contracts.access.fetcher import ProviderFetcher
from orvex.contracts.access.values import Configuration, PostgresLocation, ProviderTarget

type Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Opened:
    """One opened instance: the store it owns and every service over it."""

    store: SqliteStore | PostgresStore
    audit: AuditLog
    bootstrap: Bootstrap
    directory: Directory
    reads: DirectoryReads
    attributes: Attributes
    identities: Identities
    sign_in: SignIn
    settings: Settings
    limits: QueryLimits
    oidc: OidcSignIn | None


def target(provider: Provider) -> ProviderTarget:
    """A provider as the fetcher sees it."""
    return ProviderTarget(
        name=provider.name,
        issuer_host=provider.issuer_host,
        endpoint_hosts=tuple(provider.endpoint_hosts),
        internal=provider.internal,
        internal_ranges=tuple(str(network) for network in provider.internal_ranges),
    )


class _Fetching:
    """The internal `Fetcher` over the composer's `ProviderFetcher`."""

    def __init__(self, fetcher: ProviderFetcher) -> None:
        self._fetcher = fetcher

    def get(self, provider: Provider, url: str) -> bytes:
        return self._fetcher.get(target(provider), url)

    def post_form(
        self, provider: Provider, url: str, form: Mapping[str, str], basic: tuple[str, str]
    ) -> bytes:
        return self._fetcher.post_form(target(provider), url, form, basic)


def _store(configuration: Configuration) -> SqliteStore | PostgresStore:
    location = configuration.store
    if isinstance(location, PostgresLocation):
        if location.schema is None:
            settings = read_settings(location.connection, location.password)
        else:
            settings = read_settings(location.connection, location.password, schema=location.schema)
        return PostgresStore(settings)
    return SqliteStore(Path(location.path))


def _oidc_configuration(configuration: Configuration) -> oidc_configuration.Configuration:
    setup = configuration.oidc
    if setup is None:
        return oidc_configuration.Configuration({}, ())
    try:
        document = tomllib.loads(setup.document)
    except tomllib.TOMLDecodeError as error:
        raise AccessError(
            OIDC_NOT_CONFIGURED, "The sign-in configuration cannot be read.", {"setting": "file"}
        ) from error
    return oidc_configuration.parsed(document, setup.environment)


def _composed(
    store: SqliteStore | PostgresStore,
    configuration: Configuration,
    clock: Clock,
    fetcher: ProviderFetcher | None,
) -> Opened:
    shared: Store = store
    audit = open_audit_log(
        shared, Path(configuration.audit_key), Path(configuration.audit_anchors), clock
    )
    providers = _oidc_configuration(configuration)
    oidc = None
    if providers.providers:
        if fetcher is None:
            raise AccessError(
                OIDC_NOT_CONFIGURED,
                "The sign-in providers cannot be reached from this process.",
                {"setting": "providers"},
            )
        oidc = OidcSignIn(shared, audit, providers, _Fetching(fetcher), clock)
        oidc.start_up()
    return Opened(
        store=store,
        audit=audit,
        bootstrap=Bootstrap(shared, Path(configuration.recovery_file), audit, clock),
        directory=Directory(shared, audit, clock),
        reads=DirectoryReads(shared, audit, clock),
        attributes=Attributes(shared, audit, clock),
        identities=Identities(shared, audit, providers, clock),
        sign_in=SignIn(shared, audit, clock),
        settings=Settings(shared, audit, clock),
        limits=QueryLimits(shared, audit),
        oidc=oidc,
    )


def open_instance(
    configuration: Configuration, clock: Clock, fetcher: ProviderFetcher | None
) -> Opened:
    """The instance `configuration` describes, opened; its store is closed if opening fails."""
    store = _store(configuration)
    try:
        return _composed(store, configuration, clock, fetcher)
    except BaseException:
        store.close()
        raise
