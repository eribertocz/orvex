"""The required checks in checks.toml against main's effective protection (#267), on recorded
responses of each kind."""

from __future__ import annotations

from typing import Any

import pytest

from tools.ci import protection
from tools.repository import REPOSITORY_ROOT

REPO = "owner/repo"


def branch(enabled: bool, contexts: list[str] = (), level: str = "everyone") -> dict[str, Any]:  # type: ignore[assignment]
    return {
        "protected": True,
        "protection": {
            "enabled": enabled,
            "required_status_checks": {
                "enforcement_level": level if enabled else "off",
                "contexts": list(contexts),
                "checks": [{"context": c, "app_id": None} for c in contexts],
            },
        },
    }


def ruleset(enforcement: str, contexts: list[str]) -> dict[str, Any]:
    return {
        "enforcement": enforcement,
        "rules": [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
            },
        ],
    }


def recorded(responses: dict[str, Any]) -> protection.Fetch:
    def fetch(path: str) -> Any:  # noqa: ANN401 - parsed JSON
        for prefix, response in responses.items():
            if path.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                return response
        raise protection.ProtectionError(f"unexpected request {path}")

    return fetch


RULES = f"repos/{REPO}/rules/branches/main"
BRANCH = f"repos/{REPO}/branches/main"


def status_rule(ruleset_id: int) -> dict[str, Any]:
    return {"type": "required_status_checks", "ruleset_id": ruleset_id}


def test_a_ruleset_alone_with_classic_protection_off() -> None:
    fetch = recorded(
        {
            BRANCH: branch(False),
            RULES: [{"type": "deletion", "ruleset_id": 1}, status_rule(1)],
            f"repos/{REPO}/rulesets/1": ruleset("active", ["a", "b"]),
        }
    )
    assert protection.effective_required(REPO, fetch) == {"a", "b"}


def test_contexts_from_two_rulesets_and_classic_protection_are_joined() -> None:
    fetch = recorded(
        {
            BRANCH: branch(True, ["c"]),
            RULES: [status_rule(1), status_rule(2)],
            f"repos/{REPO}/rulesets/1": ruleset("active", ["a"]),
            f"repos/{REPO}/rulesets/2": ruleset("active", ["b", "a"]),
        }
    )
    assert protection.effective_required(REPO, fetch) == {"a", "b", "c"}


def test_a_ruleset_that_is_not_active_does_not_count() -> None:
    fetch = recorded(
        {
            BRANCH: branch(False),
            RULES: [status_rule(1), status_rule(2)],
            f"repos/{REPO}/rulesets/1": ruleset("evaluate", ["a"]),
            f"repos/{REPO}/rulesets/2": ruleset("disabled", ["b"]),
        }
    )
    assert protection.effective_required(REPO, fetch) == set()


def test_classic_checks_with_enforcement_off_do_not_count() -> None:
    fetch = recorded({BRANCH: branch(True, ["c"], level="off"), RULES: []})
    assert protection.effective_required(REPO, fetch) == set()


@pytest.mark.parametrize(
    "broken",
    [
        {BRANCH: protection.ProtectionError("403"), RULES: []},
        {BRANCH: branch(False), RULES: protection.ProtectionError("500")},
        {BRANCH: branch(False), RULES: [status_rule(1)]},  # the ruleset itself is unreadable
        {BRANCH: branch(False), RULES: {"message": "Not Found"}},
    ],
)
def test_a_source_that_cannot_be_read_fails_closed(broken: dict[str, Any]) -> None:
    with pytest.raises(protection.ProtectionError):
        protection.effective_required(REPO, recorded(broken))


def test_every_difference_is_named() -> None:
    found = protection.differences({"a", "b"}, {"b", "c"})
    assert found == [
        "main requires c, which checks.toml does not mark required",
        "checks.toml marks a required, which main does not require",
    ]
    assert protection.differences({"a"}, {"a"}) == []


def test_the_list_marks_the_required_checks_of_this_repository() -> None:
    listed = protection.listed_required(REPOSITORY_ROOT / "tools" / "ci" / "checks.toml")
    assert {"python-tests-3.15", "python-tests-3.14", "governance", "web-build"} <= listed
    assert not {"docker", "load-baseline", "release-build"} & listed


def test_without_a_token_it_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert protection.main() == 1
