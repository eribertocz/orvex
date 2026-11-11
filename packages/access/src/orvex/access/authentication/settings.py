"""The administrator's sign-in settings (application, #438): session lifetimes, the public origin
and the trusted proxies, each validated before it is stored.

- **Lifetimes.** A session ends 12 hours after sign-in, or after 1 hour without a request, by
  default. The absolute lifetime is 15 minutes to 30 days; the idle one is 5 minutes up to the
  absolute one.
- **Public origin.** The `https://host[:port]` that browsers use to reach the instance. A
  state-changing request whose `Origin` is not exactly this one is refused, and until it is set,
  every one is (fail closed).
- **Trusted proxies.** The addresses or networks whose `X-Forwarded-For` is believed, at most 64,
  none by default.
- Only an active administrator changes them, and every change is audited
  (`instance.settings_changed`, naming the settings, never their values).
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from orvex.access.audit.log import AuditLog
from orvex.access.audit.operation import audited
from orvex.access.directory import rules
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.rules import refused
from orvex.access.errors import VALIDATION_FAILED, AccessError
from orvex.access.storage.store import Store, Transaction

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

ABSOLUTE_RANGE = (timedelta(minutes=15), timedelta(days=30))
IDLE_MINIMUM = timedelta(minutes=5)
MAXIMUM_PROXIES = 64
_ORIGIN_LENGTH = 300


@dataclass(frozen=True, slots=True)
class SignInSettings:
    absolute: timedelta
    idle: timedelta
    public_origin: str | None
    trusted_proxies: tuple[Network, ...]


def _invalid(setting: str) -> AccessError:
    return AccessError(VALIDATION_FAILED, f"The {setting} is not valid.", {"field": setting})


def checked_lifetimes(absolute: timedelta, idle: timedelta) -> tuple[timedelta, timedelta]:
    low, high = ABSOLUTE_RANGE
    if not low <= absolute <= high or absolute.total_seconds() % 1:
        raise _invalid("absolute_lifetime")
    if not IDLE_MINIMUM <= idle <= absolute or idle.total_seconds() % 1:
        raise _invalid("idle_lifetime")
    return absolute, idle


def checked_origin(origin: str) -> str:
    """`https://host[:port]`, lower case, the default port left out; nothing else."""
    if len(origin) > _ORIGIN_LENGTH or origin != origin.strip() or not origin.isascii():
        raise _invalid("public_origin")
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError as error:
        raise _invalid("public_origin") from error
    host = parts.hostname
    if (
        parts.scheme.lower() != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or origin.endswith(("?", "#"))
        or port == 0
    ):
        raise _invalid("public_origin")
    shown = f"[{host}]" if ":" in host else host
    return f"https://{shown}" + ("" if port in (None, 443) else f":{port}")


def checked_proxies(proxies: Sequence[str]) -> tuple[Network, ...]:
    if isinstance(proxies, str) or len(proxies) > MAXIMUM_PROXIES:
        raise _invalid("trusted_proxies")
    try:
        found = tuple(ipaddress.ip_network(item, strict=True) for item in proxies)
    except (TypeError, ValueError) as error:
        raise _invalid("trusted_proxies") from error
    return tuple(dict.fromkeys(found))


def read(transaction: Transaction) -> SignInSettings:
    ((absolute, idle, origin, proxies),) = transaction.rows(
        "SELECT absolute_seconds, idle_seconds, public_origin, trusted_proxies "
        "FROM sign_in_settings WHERE id = 1"
    )
    networks = json.loads(str(proxies))
    return SignInSettings(
        absolute=timedelta(seconds=int(str(absolute))),
        idle=timedelta(seconds=int(str(idle))),
        public_origin=None if origin is None else checked_origin(str(origin)),
        trusted_proxies=checked_proxies(networks if isinstance(networks, list) else ""),
    )


class Settings:
    """Read the sign-in settings; change them as an administrator."""

    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def current(self) -> SignInSettings:
        with self._store.transaction(read_only=True) as transaction:
            return read(transaction)

    def change(
        self,
        actor: str,
        *,
        absolute: timedelta | None = None,
        idle: timedelta | None = None,
        public_origin: str | None = None,
        trusted_proxies: Sequence[str] | None = None,
    ) -> SignInSettings:
        """Change the settings given; the others keep their values."""

        def work(transaction: Transaction) -> SignInSettings:
            refused(rules.manage_users(directory_sql.caller(transaction, actor)))
            now = read(transaction)
            lifetimes = checked_lifetimes(
                now.absolute if absolute is None else absolute,
                now.idle if idle is None else idle,
            )
            origin = now.public_origin if public_origin is None else checked_origin(public_origin)
            proxies = (
                now.trusted_proxies if trusted_proxies is None else checked_proxies(trusted_proxies)
            )
            new = SignInSettings(lifetimes[0], lifetimes[1], origin, proxies)
            names = [
                name
                for name, before, after in (
                    ("absolute_lifetime", now.absolute, new.absolute),
                    ("idle_lifetime", now.idle, new.idle),
                    ("public_origin", now.public_origin, new.public_origin),
                    ("trusted_proxies", now.trusted_proxies, new.trusted_proxies),
                )
                if before != after
            ]
            if not names:
                return new
            transaction.run(
                "UPDATE sign_in_settings SET absolute_seconds = :absolute, "
                "idle_seconds = :idle, public_origin = :origin, trusted_proxies = :proxies "
                "WHERE id = 1",
                absolute=int(new.absolute.total_seconds()),
                idle=int(new.idle.total_seconds()),
                origin=new.public_origin,
                proxies=json.dumps([str(network) for network in new.trusted_proxies]),
            )
            self._audit.record(transaction, "instance.settings_changed", actor, {"settings": names})
            return new

        return audited(self._store, self._audit, "change_settings", actor, work)
