"""Provider groups to project groups (application, #439): only through the administrator's explicit
mapping, and only the groups that mapping manages.

- At each sign-in and each refresh, every (project, group) the provider's mapping names is set
  to what the claims say: the user is in it when one of its rules' claim values is present.
- A group is joined only where the user already has a membership in the project, and only if the
  group exists. The mapping never grants a role; a claim value with no rule grants nothing.
- Groups a Builder manages by hand, which no rule names, are never touched.
- Every change is audited as a group change by `system`, and ends the user's other sessions
  (`authz_version`).
- **A changed mapping** (its digest differs from the one stored) ends the sessions of every user
  linked through that provider, so their groups are set again at their next sign-in. A
  provider removed from the configuration does the same.
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


def _values(claims: Mapping[str, object], claim: str) -> frozenset[str]:
    found = claims.get(claim)
    if isinstance(found, str):
        return frozenset({found})
    if isinstance(found, list):
        return frozenset(item for item in found if isinstance(item, str))
    return frozenset()


def apply(
    transaction: Transaction,
    audit: AuditLog,
    rules: Iterable[MappingRule],
    user_id: str,
    claims: Mapping[str, object],
    now: datetime,
) -> bool:
    """Set the mapped groups of `user_id` from `claims`; whether anything changed."""
    wanted: dict[tuple[str, str], bool] = {}
    for rule in rules:
        target = (rule.project, rule.group)
        wanted[target] = wanted.get(target, False) or rule.value in _values(claims, rule.claim)
    changed = False
    for (project, group), member in sorted(wanted.items()):
        if not directory_sql.group_exists(transaction, project, group):
            continue
        holds = group in directory_sql.groups_of(transaction, project, user_id)
        if member and not holds and directory_sql.role(transaction, project, user_id) is not None:
            directory_sql.insert_group_member(transaction, project, group, user_id)
            audit.record(
                transaction,
                "group.member_added",
                SYSTEM,
                {"group": group, "user": user_id},
                project=project,
            )
            changed = True
        elif not member and holds:
            directory_sql.delete_group_member(transaction, project, group, user_id)
            audit.record(
                transaction,
                "group.member_removed",
                SYSTEM,
                {"group": group, "user": user_id},
                project=project,
            )
            changed = True
    if changed:
        directory_sql.bump(transaction, user_id, instant(now))
    return changed


def digest(rules: Iterable[MappingRule]) -> str:
    canonical = sorted((rule.claim, rule.value, rule.project, rule.group) for rule in rules)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()


def reconcile(
    transaction: Transaction, audit: AuditLog, configuration: Configuration, now: datetime
) -> list[str]:
    """Compare each provider's mapping with the one stored; end the sessions of the users a
    change affects, record it, and return the providers whose mapping changed."""
    stored = {
        str(provider): str(value)
        for provider, value in transaction.rows("SELECT provider, digest FROM oidc_mapping_state")
    }
    current = {name: digest(configuration.rules_for(name)) for name in configuration.providers}
    changed = sorted(
        name for name in set(stored) | set(current) if stored.get(name) != current.get(name)
    )
    for name in changed:
        for (user,) in transaction.rows(
            "SELECT DISTINCT user_id FROM oidc_identities WHERE provider = :provider",
            provider=name,
        ):
            directory_sql.bump(transaction, str(user), instant(now))
        transaction.run("DELETE FROM oidc_mapping_state WHERE provider = :provider", provider=name)
        if name in current:
            transaction.run(
                "INSERT INTO oidc_mapping_state (provider, digest) VALUES (:provider, :digest)",
                provider=name,
                digest=current[name],
            )
        audit.record(transaction, "instance.oidc_mapping_changed", SYSTEM, {"provider": name})
    return changed
