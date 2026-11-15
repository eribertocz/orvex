"""The security-context operations of the `api` package: each context built by the module's own
providers and crossed as its `security-context` v1 document in JSON text.

Every `InvalidContext` becomes the contract's refusal `CONTEXT_INVALID`; a fixture's stale or
malformed fingerprint is never reusable. A document that is not JSON is `CONTEXT_INVALID` too.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from orvex.access import context, fixtures, local
from orvex.contracts.access.errors import CONTEXT_INVALID, AccessError, InvalidContext
from orvex.contracts.access.values import ContextFixture, FingerprintInputs


def text(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def checked[T](work: Callable[[], T]) -> T:
    """`work()`, with an `InvalidContext` as `CONTEXT_INVALID`."""
    try:
        return work()
    except InvalidContext as error:
        raise AccessError(CONTEXT_INVALID, f"The security context is not valid: {error}") from None


def _document(document: str) -> dict[str, Any]:
    try:
        parsed = json.loads(document)
    except ValueError:
        raise InvalidContext("the document is not JSON") from None
    if not isinstance(parsed, dict):
        raise InvalidContext("the document is not an object")
    return parsed


def local_context(
    project: str, authenticated: bool, policy_fingerprint: str, mapping: Mapping[str, str]
) -> str:
    provider = local.LocalContextProvider(project, policy_fingerprint, mapping)
    request = local.LocalRequest(project, authenticated=authenticated)
    return checked(lambda: text(provider.context_for(request).document()))


def _provider(fixture: ContextFixture) -> fixtures.FixtureContextProvider:
    provider = fixtures.FixtureContextProvider(
        fixture.policy_fingerprint, fixture.mapping_snapshots
    )
    for member in fixture.members:
        provider.add(
            member.user,
            member.project,
            fixtures.Member(member.role, member.groups, dict(member.attributes)),
        )
    return provider


def fixture_context(fixture: ContextFixture, user: str, project: str) -> str:
    provider = _provider(fixture)
    request = fixtures.Request(user, project)
    return checked(lambda: text(provider.context_for(request).document()))


def fixture_may_reuse(stored: str | None, fixture: ContextFixture, user: str, project: str) -> bool:
    return context.may_reuse(stored, _provider(fixture), fixtures.Request(user, project))


def read_context(document: str) -> str:
    return checked(lambda: text(context.read_context(_document(document)).document()))


def permission_fingerprint(inputs: FingerprintInputs) -> str:
    return checked(
        lambda: context.permission_fingerprint(
            user=inputs.user,
            project=inputs.project,
            role=inputs.role,
            groups=inputs.groups,
            attributes=_document(inputs.attributes_document),
            policy_fingerprint=inputs.policy_fingerprint,
            mapping_snapshots=inputs.mapping_snapshots,
        )
    )
