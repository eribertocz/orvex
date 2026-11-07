"""P2-27a (#363): the one local Builder exists only for an authenticated local request."""

from __future__ import annotations

import pytest
from orvex.access.context import InvalidContext
from orvex.access.local import LocalContextProvider, LocalRequest

POLICIES = "sha256:" + "0" * 64


def test_an_authenticated_local_request_is_the_project_s_builder_with_no_groups() -> None:
    provider = LocalContextProvider("project-1", POLICIES, {"lake": "3"})
    context = provider.context_for(LocalRequest("project-1", authenticated=True))
    assert (context.user, context.role, context.groups) == ("local", "builder", ())
    assert dict(context.attributes) == {}


@pytest.mark.parametrize(
    "request_",
    [LocalRequest("project-1"), LocalRequest("other", authenticated=True), object(), None],
)
def test_anything_else_has_no_context(request_: object) -> None:
    provider = LocalContextProvider("project-1", POLICIES, {"lake": "3"})
    with pytest.raises(InvalidContext):
        provider.context_for(request_)


def test_the_fingerprint_follows_the_policies_and_the_snapshot_read() -> None:
    request = LocalRequest("project-1", authenticated=True)
    first = LocalContextProvider("project-1", POLICIES, {"lake": "3"}).context_for(request)
    later = LocalContextProvider("project-1", POLICIES, {"lake": "4"}).context_for(request)
    other = LocalContextProvider("project-1", "sha256:" + "1" * 64, {"lake": "3"})
    assert first.permission_fingerprint != later.permission_fingerprint
    assert first.permission_fingerprint != other.context_for(request).permission_fingerprint
