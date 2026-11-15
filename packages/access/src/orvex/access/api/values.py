"""The access module's records as the contract's values (`orvex.contracts.access.values`):
each field copied, nothing computed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orvex.access.authentication.query_limits import RoleLimit
from orvex.access.authentication.service import SignedIn
from orvex.access.authentication.sessions import Session
from orvex.access.authentication.settings import SignInSettings
from orvex.access.directory.reads import Found, Member, Page
from orvex.access.directory.records import NewUser, User
from orvex.access.oidc.identities import Link
from orvex.access.oidc.service import Started
from orvex.contracts.access import values


def user(found: User) -> values.User:
    return values.User(
        id=found.id,
        user_name=found.user_name,
        display_name=found.display_name,
        state=found.state,
        administrator=found.administrator,
        authz_version=found.authz_version,
        created_at=found.created_at,
        changed_at=found.changed_at,
    )


def new_user(made: NewUser) -> values.NewUser:
    return values.NewUser(user(made.user), made.temporary_password)


def signed_in(signed: SignedIn) -> values.SignedIn:
    return values.SignedIn(signed.session_id, user(signed.user), signed.password_change_only)


def session(live: Session) -> values.Session:
    return values.Session(user(live.user), live.password_change_only)


def settings(current: SignInSettings) -> values.SignInSettings:
    return values.SignInSettings(
        absolute=current.absolute,
        idle=current.idle,
        public_origin=current.public_origin,
        trusted_proxies=tuple(str(network) for network in current.trusted_proxies),
    )


def limits(current: Mapping[str, RoleLimit]) -> dict[str, values.RoleLimit]:
    return {
        role: values.RoleLimit(limit.max_concurrent, limit.max_run_ms)
        for role, limit in current.items()
    }


def role_limits(asked: Mapping[str, values.RoleLimit]) -> dict[str, RoleLimit]:
    return {
        role: RoleLimit(limit.max_concurrent, limit.max_run_ms) for role, limit in asked.items()
    }


def page(found: Page) -> values.UserPage:
    return values.UserPage(tuple(user(each) for each in found.users), found.after)


def found_user(found: Found) -> values.FoundUser:
    return values.FoundUser(found.id, found.display_name)


def members(found: list[Member]) -> tuple[values.Member, ...]:
    return tuple(values.Member(user(member.user), member.role) for member in found)


def groups(found: Mapping[str, list[str]]) -> dict[str, tuple[str, ...]]:
    return {name: tuple(members) for name, members in found.items()}


def attributes(found: Mapping[str, Mapping[str, Any]]) -> dict[str, values.Attribute]:
    return {
        name: values.Attribute(
            type=attribute["type"],
            value=attribute.get("value"),
            values=None if "values" not in attribute else tuple(attribute["values"]),
        )
        for name, attribute in found.items()
    }


def links(found: list[Link]) -> tuple[values.IdentityLink, ...]:
    return tuple(
        values.IdentityLink(link.id, link.provider, link.subject, link.linked_at) for link in found
    )


def started(found: Started) -> values.OidcStarted:
    return values.OidcStarted(found.location, found.reference)
