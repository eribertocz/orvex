"""#446 AC5 and AC3 over HTTP on SQLite and PostgreSQL: OIDC links are made and removed by an
opaque id, never by a subject in a route; two Builders removing each other at once never leave
the project without one."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.support.access_directory import audit_events

from .admin_instance import ORIGIN, SAME, AdminInstance, admin_instance

A = "/api/v1/admin"


@pytest.fixture(params=["sqlite", "postgres"])
def instance(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[AdminInstance]:
    with admin_instance(request.param, tmp_path) as made:
        yield made


def code(answer: Any) -> tuple[int, str]:  # noqa: ANN401
    return answer.status_code, answer.json()["error"]["code"]


def signs_in(instance: AdminInstance, subject: str) -> bool:
    client = TestClient(instance.app, base_url=ORIGIN, follow_redirects=False)
    started = client.get("/api/v1/auth/oidc/corp/start")
    answer = instance.provider.authorize(started.headers["location"], subject)
    return bool(client.get("/api/v1/auth/oidc/corp/callback", params=answer).status_code == 200)


def test_an_identity_is_linked_and_unlinked_by_its_opaque_id(
    instance: AdminInstance, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ana, ben = instance.person("ana"), instance.person("ben")
    root = instance.client("root")
    made = root.post(f"{A}/users/{ana}/identities", json={"provider": "corp", "subject": "sub-ana"},
                     headers=SAME)  # fmt: skip
    assert made.status_code == 201, made.text
    link = made.json()["id"]
    assert len(link) == 32
    assert signs_in(instance, "sub-ana")
    listed = root.get(f"{A}/users/{ana}/identities").json()["identities"]
    assert [(i["id"], i["provider"], i["subject"]) for i in listed] == [(link, "corp", "sub-ana")]
    taken = root.post(f"{A}/users/{ben}/identities", json={"provider": "corp",
                      "subject": "sub-ana"}, headers=SAME)  # fmt: skip
    assert code(taken) == (409, "IDENTITY_TAKEN")
    for user, wrong in ((ben, link), (ana, "0" * 32)):  # another user's id, an unknown one
        assert code(root.delete(f"{A}/users/{user}/identities/{wrong}", headers=SAME)) == (
            404,
            "IDENTITY_NOT_FOUND",
        )
    assert root.delete(f"{A}/users/{ana}/identities/{link}", headers=SAME).status_code == 204
    assert not signs_in(instance, "sub-ana")
    assert "sub-ana" not in str(audit_events(instance.store))
    assert "sub-ana" not in caplog.text  # nor in any log line


def test_two_builders_removing_each_other_at_once_leave_one(instance: AdminInstance) -> None:
    first, second = instance.person("first"), instance.person("second")
    instance.directory.add_membership(instance.admin, "project-1", first, "builder")
    instance.directory.add_membership(first, "project-1", second, "builder")
    clients = {first: instance.client("first"), second: instance.client("second")}
    gate = threading.Barrier(2)
    answers: dict[str, int] = {}

    def remove(actor: str, target: str) -> None:
        gate.wait(10)
        path = f"{A}/projects/project-1/members/{target}"
        answers[actor] = clients[actor].delete(path, headers=SAME).status_code

    threads = [
        threading.Thread(target=remove, args=(first, second)),
        threading.Thread(target=remove, args=(second, first)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(answers.values()) != [204, 204]
    assert 204 in answers.values()
    builders = [
        m for m in instance.client("root").get(f"{A}/projects/project-1/members").json()["members"]
        if m["role"] == "builder"
    ]  # fmt: skip
    assert len(builders) == 1
