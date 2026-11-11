"""#440 AC2, AC5, AC7, AC8 over HTTP in server mode: the semantic routes run for the session's
user with a context built at each request. A next page under changed permissions is refused, a
missing attribute gives zero rows, a caller without a role gets nothing, and each error is
written for the caller's audience."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from orvex.access.audit import open_audit_log
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.directory import Bootstrap, Directory
from orvex.access.directory.attributes import Attributes
from orvex.access.storage import SqliteStore
from orvex.server import semantic_http
from orvex.server.cli import served_project
from orvex.server.server_app import ServedProject, create_server_app
from orvex.server.sign_in_http import cookie_name

from tests.support.semantic_http import IDENTITY, VERSIONS, aggregate, detail, project, run
from tests.support.sign_in import PASSWORD

ORIGIN = "https://bi.example.com"
SAME = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
POLICIES = """\
version: 1
row_policies:
  - name: by_tariff
    dataset: readings
    kind: predicate
    predicate: "tariff = $tariff"
    exempt_groups: [auditors]
column_policies:
  - member: readings.kwh
    default: masked
    mask: "***"
    visible_to_groups: [finance]
"""


class Server:
    def __init__(self, tmp_path: Path) -> None:
        project(tmp_path, POLICIES)
        run(tmp_path)
        store = SqliteStore(tmp_path / "access.sqlite")
        self.store = store
        audit = open_audit_log(store, tmp_path / "audit.key", tmp_path / "anchors.log")
        self.admin = (
            Bootstrap(store, tmp_path / "recovery.key", audit)
            .create_administrator("root", "Root")
            .id
        )
        self.directory = Directory(store, audit)
        self.attributes = Attributes(store, audit)
        settings = Settings(store, audit)
        settings.change(self.admin, public_origin=ORIGIN)
        self.sign_in = SignIn(store, audit)
        semantic = semantic_http.Semantic(
            served_project(tmp_path, IDENTITY),
            semantic_http.server_context(store, audit),
            semantic_http.session_settings(),
        )
        self.app = create_server_app(
            VERSIONS,
            self.sign_in,
            settings,
            IDENTITY.slug,
            served=ServedProject("project-1", semantic, store),
        )
        self.builder = self.person("builder")
        self.directory.add_membership(self.admin, "project-1", self.builder, "builder")
        for group in ("finance", "auditors"):
            self.directory.create_group(self.builder, "project-1", group)

    def person(self, name: str) -> str:
        created = self.directory.create_user(self.admin, name, name.title())
        first = self.sign_in.login(name, created.temporary_password, "192.0.2.9")
        self.sign_in.change_password(
            first.session_id, created.temporary_password, PASSWORD, "192.0.2.9"
        )
        return created.id

    def client(self, name: str) -> TestClient:
        http = TestClient(self.app, base_url=ORIGIN)
        answer = http.post(
            "/api/v1/auth/login", json={"user_name": name, "password": PASSWORD}, headers=SAME
        )
        assert answer.status_code == 200, answer.text
        assert http.cookies.get(cookie_name(IDENTITY.slug))
        return http

    def viewer(self, name: str, *groups: str, tariff: str | None = "residential") -> str:
        user = self.person(name)
        self.directory.add_membership(self.builder, "project-1", user, "viewer")
        for group in groups:
            self.directory.add_group_member(self.builder, "project-1", group, user)
        if tariff is not None:
            self.attributes.set(self.builder, "project-1", user, "tariff", "text", tariff)
        return user


def ask(http: TestClient, query: dict[str, Any]) -> Any:  # noqa: ANN401
    return http.post("/api/v1/semantic/query", json=query, headers=SAME)


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


def test_a_viewer_sees_what_their_groups_and_attributes_allow(server: Server) -> None:
    server.viewer("ana", "finance")
    answer = ask(server.client("ana"), detail(10))
    assert answer.status_code == 200, answer.text
    rows = answer.json()["result"]["rows"]
    assert [row[0] for row in rows] == [1, 2]  # residential only
    assert rows[0][1] != "***"  # finance sees kwh


def test_a_missing_attribute_gives_zero_rows(server: Server) -> None:
    server.viewer("ben", "finance", tariff=None)
    answer = ask(server.client("ben"), detail(10))
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["rows"] == []
    notices = str(answer.json())
    assert "POLICY_ATTRIBUTE_MISSING" in notices


def test_a_next_page_under_changed_permissions_is_refused(server: Server) -> None:
    ana = server.viewer("ana", "auditors")
    http = server.client("ana")
    first = ask(http, detail(2))
    assert first.status_code == 200, first.text
    after = first.json()["result"]["page"]["next"]
    server.directory.remove_group_member(server.builder, "project-1", "auditors", ana)
    ended = ask(http, detail(2, after))  # a group change ends the session itself (#438)
    assert (ended.status_code, ended.json()["error"]["code"]) == (401, "SESSION_EXPIRED")
    second = ask(server.client("ana"), detail(2, after))  # signed in again, the old cursor
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "PAGE_CURSOR_INVALID"
    assert "result" not in second.json()


def test_a_next_page_after_an_attribute_change_is_refused(server: Server) -> None:
    ana = server.viewer("ana", "auditors")
    http = server.client("ana")
    after = ask(http, detail(2)).json()["result"]["page"]["next"]
    server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "commercial")
    assert ask(http, detail(2, after)).json()["error"]["code"] == "PAGE_CURSOR_INVALID"


def test_the_same_permissions_serve_the_next_page(server: Server) -> None:
    server.viewer("ana", "auditors")
    http = server.client("ana")
    after = ask(http, detail(2)).json()["result"]["page"]["next"]
    second = ask(http, detail(2, after))
    assert second.status_code == 200, second.text
    assert [row[0] for row in second.json()["result"]["rows"]] == [3, 4]


def test_a_user_without_a_role_gets_nothing(server: Server) -> None:
    server.person("eve")
    answer = ask(server.client("eve"), aggregate())
    assert (answer.status_code, answer.json()["error"]["code"]) == (403, "ACCESS_FORBIDDEN")
    catalog = server.client("eve").get("/api/v1/semantic/catalog")
    assert catalog.status_code == 403


def test_each_error_is_written_for_the_callers_audience(server: Server) -> None:
    server.viewer("ana")
    unknown = {**aggregate(), "metrics": [{"metric": "nothing"}]}
    as_viewer = ask(server.client("ana"), unknown).json()["error"]
    as_builder = ask(server.client("builder"), unknown).json()["error"]
    assert as_viewer["code"] == as_builder["code"] == "MEMBER_NOT_FOUND"
    assert as_viewer["semantic"]["audience"] == "other"
    assert as_builder["semantic"]["audience"] == "builder"


def test_a_mistyped_attribute_gives_zero_rows_never_all(server: Server) -> None:
    ana = server.viewer("ana", "finance")
    server.attributes.set(server.builder, "project-1", ana, "tariff", "integer", "5")
    answer = ask(server.client("ana"), detail(10))
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["rows"] == []  # an integer never equals text: no row
