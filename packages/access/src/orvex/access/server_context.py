"""The caller of server mode (#440, Q6 A): a `security-context` v1 built for each request from the
directory as it is at that moment.

- **One consistent state.** The user's state, role, groups and attributes are read in one
  read-only transaction, so a context never mixes values from before and after a concurrent
  change.
- **No role, no context.** An unknown or deactivated user, or one without a role in the project,
  is `ACCESS_FORBIDDEN`, audited (`build_context`). The administrator flag never enters a
  context and grants nothing here.
- **Fail closed on read.** An attribute that fails the contract's checks when read
  (`UnreadableAttribute`) builds no context: `ACCESS_FORBIDDEN`, audited without its value.
- **The rest of the fingerprint.** The server supplies the policy model's fingerprint and the
  snapshot of every managed-lake table the policies read, pinned once for the request. The
  context's fingerprint and the query's execution therefore use the same values; `access`
  never reads the lake.
- Attributes come through `AttributeSource`s, the project's own first (`attributes.merged`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from orvex.access.audit.log import AuditLog
from orvex.access.context import (
    SecurityContext,
    normalize_list,
    permission_fingerprint,
    read_context,
)
from orvex.access.directory import statements as sql
from orvex.access.directory.attributes import (
    AttributeSource,
    ProjectAttributes,
    UnreadableAttribute,
    merged,
)
from orvex.access.errors import ACCESS_FORBIDDEN, AccessError
from orvex.access.storage.store import Store

type Audience = Literal["builder", "other"]


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """An authenticated server-mode request (#438 session) for `project`."""

    user: str
    project: str


def _forbidden() -> AccessError:
    return AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")


class ServerContextProvider:
    """Contexts for one project, on the policy model and lake snapshots the server pinned."""

    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        policy_fingerprint: str,
        mapping_snapshots: Mapping[str, str],
        sources: tuple[AttributeSource, ...] = (ProjectAttributes(),),
    ) -> None:
        self._store = store
        self._audit = audit
        self.policy_fingerprint = policy_fingerprint
        self.mapping_snapshots = dict(mapping_snapshots)
        self._sources = sources

    def context_for(self, request: object) -> SecurityContext:
        if not isinstance(request, ServerRequest):
            raise _forbidden()
        user, project = request.user, request.project
        try:
            with self._store.transaction(read_only=True) as transaction:
                found = sql.user(transaction, user)
                role = sql.role(transaction, project, user) if found is not None else None
                if found is None or not found.active or role is None:
                    raise _forbidden()
                groups = normalize_list(sql.groups_of(transaction, project, user))
                attributes = merged(self._sources, transaction, user, project)
        except (AccessError, UnreadableAttribute) as error:
            self._audit.refusal("build_context", user, ACCESS_FORBIDDEN, project=project)
            raise _forbidden() from error
        fingerprint = permission_fingerprint(
            user=user,
            project=project,
            role=role,
            groups=groups,
            attributes=attributes,
            policy_fingerprint=self.policy_fingerprint,
            mapping_snapshots=self.mapping_snapshots,
        )
        return read_context(
            {
                "kind": "security_context",
                "version": 1,
                "user": user,
                "project": project,
                "role": role,
                "groups": groups,
                "attributes": attributes,
                "permission_fingerprint": fingerprint,
            }
        )


def audience_of(store: Store, request: ServerRequest) -> Audience:
    """Whom the caller's errors and explanations are written for: `builder` for a Builder of the
    project, the ordinary audience for anyone else (never fixed)."""
    with store.transaction(read_only=True) as transaction:
        found = sql.user(transaction, request.user)
        role = sql.role(transaction, request.project, request.user)
    return "builder" if found is not None and found.active and role == "builder" else "other"
