"""#440: `$user_id` is the context's own user. It is bound from `user`, and an attribute of that
name that names anyone else refuses the context, never read as either user."""

from __future__ import annotations

from typing import Any

import pytest
from orvex.semantic.policies import read_caller
from orvex.semantic.policies.caller import InvalidContext


def context(**attributes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "kind": "security_context",
        "version": 1,
        "user": "u-1",
        "project": "p1",
        "role": "viewer",
        "groups": [],
        "attributes": attributes,
        "permission_fingerprint": "sha256:" + "0" * 64,
    }


def test_user_id_is_the_contexts_own_user() -> None:
    assert read_caller(context()).attributes["user_id"] == ("text", "u-1")
    same = read_caller(context(user_id={"type": "text", "value": "u-1"}))
    assert same.attributes["user_id"] == ("text", "u-1")


@pytest.mark.parametrize(
    "value",
    [{"type": "text", "value": "u-2"}, {"type": "integer", "value": "1"}],
    ids=["another-user", "another-type"],
)
def test_a_user_id_naming_anyone_else_is_refused(value: dict[str, Any]) -> None:
    with pytest.raises(InvalidContext):
        read_caller(context(user_id=value))
