"""Provider groups to project groups (application, #439): only through the administrator's
explicit mapping, and only memberships the mapping itself granted.

- **Provenance.** Every group membership the mapping grants is recorded as a grant (user,
  project, group, provider). Only a grant is ever taken back by the mapping. A membership a
  Builder set by hand is never touched, even in a group the mapping names.
- **At each sign-in and refresh**, for each (project, group) the provider's rules name: the user
  joins when one of its rules' claim values is present, the group exists, and the user already
  has a membership in the project (the mapping never grants a role). A grant whose claim is gone,
  or whose rule is gone, is taken back. A claim value with no rule grants nothing.
- **Nowhere else.** Grants live only as long as provider sessions keep them fresh: a sign-in by
  any other method takes back every grant first (`revoke`), so provider groups never outlive
  `group_refresh` in a session that is not refreshed.
- **A changed mapping** (its digest differs from the one stored), and a provider removed from the
  configuration, take back the grants no rule supports any longer. They also end the sessions of
  every user linked through that provider.
- Every change is audited as a group change by `system`, and ends the user's sessions
  (`authz_version`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime

from orvex.access.audit.log import SYSTEM, AuditLog
from orvex.access.directory import statements as directory_sql
from orvex.access.oidc.configuration import Configuration, MappingRule
from orvex.access.storage.instants import instant
from orvex.access.storage.store import Transaction

type Target = tuple[str, str]


def _values(claims: Mapping[str, object], claim: str) -> frozenset[str]:
    found = claims.get(claim)
    if isinstance(found, str):
        return frozenset({found})
    if isinstance(found, list):
        return frozenset(item for item in found if isinstance(item, str))
    return frozenset()


def _grants(
    transaction: Transaction, user_id: str, provider: str | None = None
) -> dict[Target, str]:
    rows = transaction.rows(
        "SELECT project, group_name, provider FROM oidc_grants WHERE user_id = :user",
        user=user_id,
    )
    return {
        (str(project), str(group)): str(owner)
        for project, group, owner in rows
        if provider is None or str(owner) == provider
    }


def _grant(
    transaction: Transaction, audit: AuditLog, user_id: str, target: Target, provider: str
) -> None:
    project, group = target
    directory_sql.insert_group_member(transaction, project, group, user_id)
    transaction.run(
        "INSERT INTO oidc_grants (user_id, project, group_name, provider) "
        "VALUES (:user, :project, :group, :provider)",
        user=user_id,
        project=project,
        group=group,
        provider=provider,
    )
    audit.record(
        transaction,
        "group.member_added",
        SYSTEM,
        {"group": group, "user": user_id},
        project=project,
    )


def _take_back(transaction: Transaction, audit: AuditLog, user_id: str, target: Target) -> None:
    project, group = target
    transaction.run(
        "DELETE FROM oidc_grants WHERE user_id = :user AND project = :project "
        "AND group_name = :group",
        user=user_id,
        project=project,
        group=group,
    )
    if directory_sql.delete_group_member(transaction, project, group, user_id):
        audit.record(
            transaction,
            "group.member_removed",
            SYSTEM,
            {"group": group, "user": user_id},
            project=project,
        )


def apply(
    transaction: Transaction,
    audit: AuditLog,
    provider: str,
    rules: Iterable[MappingRule],
    user_id: str,
    claims: Mapping[str, object],
    now: datetime,
) -> bool:
    """Set the user's grants from `provider`'s claims; whether anything changed."""
    wanted: dict[Target, bool] = {}
    for rule in rules:
        target = (rule.project, rule.group)
        wanted[target] = wanted.get(target, False) or rule.value in _values(claims, rule.claim)
    held = _grants(transaction, user_id, provider)
    everyone = _grants(transaction, user_id)
    changed = False
    for target in sorted(set(held) - set(wanted)):  # its rule is gone
        _take_back(transaction, audit, user_id, target)
        changed = True
    for target, member in sorted(wanted.items()):
        project, group = target
        if member and target not in held:
            joinable = (
                target not in everyone  # another provider's grant
                and directory_sql.group_exists(transaction, project, group)
                and directory_sql.role(transaction, project, user_id) is not None
                and group not in directory_sql.groups_of(transaction, project, user_id)
            )
            if joinable:
                _grant(transaction, audit, user_id, target, provider)
                changed = True
        elif not member and target in held:
            _take_back(transaction, audit, user_id, target)
            changed = True
    if changed:
        directory_sql.bump(transaction, user_id, instant(now))
    return changed


def revoke(transaction: Transaction, audit: AuditLog, user_id: str, now: datetime) -> bool:
    """Take back every grant the user holds from any provider; whether there was any."""
    held = _grants(transaction, user_id)
    for target in sorted(held):
        _take_back(transaction, audit, user_id, target)
    if held:
        directory_sql.bump(transaction, user_id, instant(now))
    return bool(held)


def digest(rules: Iterable[MappingRule]) -> str:
    canonical = sorted((rule.claim, rule.value, rule.project, rule.group) for rule in rules)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()


def reconcile(
    transaction: Transaction, audit: AuditLog, configuration: Configuration, now: datetime
) -> list[str]:
    """Compare each provider's mapping with the one stored. For each change: take back the grants
    no current rule supports, end the sessions of the users linked through that provider, record
    it, and return the providers that changed."""
    stored = {
        str(provider): str(value)
        for provider, value in transaction.rows("SELECT provider, digest FROM oidc_mapping_state")
    }
    current = {name: digest(configuration.rules_for(name)) for name in configuration.providers}
    changed = sorted(
        name for name in set(stored) | set(current) if stored.get(name) != current.get(name)
    )
    for name in changed:
        supported = {(rule.project, rule.group) for rule in configuration.rules_for(name)}
        affected: set[str] = set()
        for user, project, group in transaction.rows(
            "SELECT user_id, project, group_name FROM oidc_grants WHERE provider = :provider",
            provider=name,
        ):
            if (str(project), str(group)) not in supported:
                _take_back(transaction, audit, str(user), (str(project), str(group)))
                affected.add(str(user))
        for (user,) in transaction.rows(
            "SELECT DISTINCT user_id FROM oidc_identities WHERE provider = :provider",
            provider=name,
        ):
            affected.add(str(user))
        for user in sorted(affected):
            directory_sql.bump(transaction, user, instant(now))
        transaction.run("DELETE FROM oidc_mapping_state WHERE provider = :provider", provider=name)
        if name in current:
            transaction.run(
                "INSERT INTO oidc_mapping_state (provider, digest) VALUES (:provider, :digest)",
                provider=name,
                digest=current[name],
            )
        audit.record(transaction, "instance.oidc_mapping_changed", SYSTEM, {"provider": name})
    return changed
