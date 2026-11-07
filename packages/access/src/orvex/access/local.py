"""The caller of local mode (`security.md` §2; P2-27a, #363).

Local mode has no login and one implicit user, the Builder of the project the server opened.
That user exists only for a request the HTTP layer has authenticated with the serve session's
token: `LocalRequest` is the proof, and nothing else yields a context. The user has no groups
and no attributes, so no policy exemption ever applies (fail closed, Owner decision on #358).

This grant is the HTTP API's own. It is not the CLI's local builder grant, and neither reaches
the other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from orvex.access.context import (
    InvalidContext,
    SecurityContext,
    permission_fingerprint,
    read_context,
)

LOCAL_USER = "local"


@dataclass(frozen=True, slots=True)
class LocalRequest:
    """A request the HTTP layer authenticated with the serve session's token, for `project`."""

    project: str
    authenticated: bool = field(default=False)


class LocalContextProvider:
    """The context of the one local Builder of `project`, built anew for each request."""

    def __init__(
        self, project: str, policy_fingerprint: str, mapping_snapshots: Mapping[str, str]
    ) -> None:
        self.project = project
        self.policy_fingerprint = policy_fingerprint
        self.mapping_snapshots = dict(mapping_snapshots)

    def context_for(self, request: object) -> SecurityContext:
        if (
            not isinstance(request, LocalRequest)
            or request.authenticated is not True
            or request.project != self.project
        ):
            raise InvalidContext("the request is not an authenticated local request")
        fingerprint = permission_fingerprint(
            user=LOCAL_USER,
            project=self.project,
            role="builder",
            groups=(),
            attributes={},
            policy_fingerprint=self.policy_fingerprint,
            mapping_snapshots=self.mapping_snapshots,
        )
        return read_context(  # validated against the contract, as every context is
            {
                "kind": "security_context",
                "version": 1,
                "user": LOCAL_USER,
                "project": self.project,
                "role": "builder",
                "groups": [],
                "attributes": {},
                "permission_fingerprint": fingerprint,
            }
        )
