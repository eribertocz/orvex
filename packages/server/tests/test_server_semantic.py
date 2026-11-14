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
from orvex.access.directory.reads import DirectoryReads
from orvex.access.storage import SqliteStore
from orvex.server import semantic_http
from orvex.server.admin_http import Administration
from orvex.server.cli import served_project
from orvex.server.server_app import ServedProject, create_server_app
from orvex.server.sign_in_http import cookie_name

from tests.support.semantic_http import (
    IDENTITY,
    VERSIONS,
    aggregate,
    detail,
    project,
    run,
    write,
)
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


MAPPED = """\
version: 1
row_policies:
  - name: mapped_tariffs
    dataset: readings
    kind: predicate
    predicate: "tariff IN (SELECT tariff FROM security.user_tariff WHERE user_id = $user_id)"
"""


class Server:
    def __init__(
        self, tmp_path: Path, policies: str = POLICIES, *, administration: bool = False
    ) -> None:
        self.folder = tmp_path
        project(tmp_path, policies)
        self.mapping({})
        run(tmp_path)
        store = SqliteStore(tmp_path / "access.sqlite")
        self.store = store
        audit = open_audit_log(store, tmp_path / "audit.key", tmp_path / "anchors.log")
        first = Bootstrap(store, tmp_path / "recovery.key", audit).create_administrator(
            "root", "Root"
        )
        self.admin = first.id
        signed = SignIn(store, audit).login("root", first.temporary_password, "192.0.2.9")
        SignIn(store, audit).change_password(
            signed.session_id, first.temporary_password, PASSWORD, "192.0.2.9"
        )
        self.directory = Directory(store, audit)
        self.attributes = Attributes(store, audit)
        settings = Settings(store, audit)
        settings.change(self.admin, public_origin=ORIGIN)
        self.settings = settings
        self.sign_in = SignIn(store, audit)
        built = semantic_http.server_context(store, audit)
        self.contexts: list[tuple[str, int]] = []  # (policy fingerprint, mapping snapshot)

        def context(request: object, policy_fingerprint: str, mapping_snapshot: int) -> Any:  # noqa: ANN401
            self.contexts.append((policy_fingerprint, mapping_snapshot))
            return built(request, policy_fingerprint, mapping_snapshot)

        self.audit = audit
        semantic = semantic_http.Semantic(
            served_project(tmp_path, IDENTITY),
            context,
            semantic_http.session_settings(),
            audit,  # server mode records the answers' events (#443, #445)
            semantic_http.server_limits(store, audit),
            identity=semantic_http.server_identity(store),
        )
        self.semantic = semantic
        self.app = create_server_app(
            VERSIONS,
            self.sign_in,
            settings,
            IDENTITY.slug,
            served=ServedProject("project-1", semantic, store),
            administration=(
                Administration(
                    self.directory,
                    DirectoryReads(store, audit),
                    self.sign_in,
                    self.attributes,
                    None,
                    frozenset({"project-1", "project-2"}),
                    audit,
                )
                if administration
                else None
            ),
        )
        self.builder = self.person("builder")
        self.directory.add_membership(self.admin, "project-1", self.builder, "builder")
        for group in ("finance", "auditors"):
            self.directory.create_group(self.builder, "project-1", group)

    def mapping(self, tariffs: dict[str, list[str]]) -> None:
        """The managed-lake table `security.user_tariff`, as the next run writes it."""
        rows = [f"('{user}', '{t}')" for user, found in tariffs.items() for t in found]
        values = ", ".join(rows or ["('nobody', 'none')"])
        write(
            self.folder,
            {
                "models/security/user_tariff.sql": (
                    f"SELECT * FROM (VALUES {values}) AS t(user_id, tariff)"
                )
            },
        )

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

    def author(self, name: str, *groups: str, tariff: str | None = "residential") -> str:
        """A member who asks free-form questions: an Author (a Viewer may not, #445)."""
        return self.member(name, *groups, tariff=tariff, role="author")

    def viewer(self, name: str, *groups: str, tariff: str | None = "residential") -> str:
        return self.member(name, *groups, tariff=tariff, role="viewer")

    def member(
        self, name: str, *groups: str, tariff: str | None = "residential", role: str = "author"
    ) -> str:
        user = self.person(name)
        self.directory.add_membership(self.builder, "project-1", user, role)
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
    server.author("ana", "finance")
    answer = ask(server.client("ana"), detail(10))
    assert answer.status_code == 200, answer.text
    rows = answer.json()["result"]["rows"]
    assert [row[0] for row in rows] == [1, 2]  # residential only
    assert rows[0][1] != "***"  # finance sees kwh


def test_a_missing_attribute_gives_zero_rows(server: Server) -> None:
    server.author("ben", "finance", tariff=None)
    answer = ask(server.client("ben"), detail(10))
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["rows"] == []
    notices = str(answer.json())
    assert "POLICY_ATTRIBUTE_MISSING" in notices


def test_a_next_page_under_changed_permissions_is_refused(server: Server) -> None:
    ana = server.author("ana", "auditors")
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
    ana = server.author("ana", "auditors")
    http = server.client("ana")
    after = ask(http, detail(2)).json()["result"]["page"]["next"]
    server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "commercial")
    assert ask(http, detail(2, after)).json()["error"]["code"] == "PAGE_CURSOR_INVALID"


def test_the_same_permissions_serve_the_next_page(server: Server) -> None:
    server.author("ana", "auditors")
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
    server.author("ana")
    unknown = {**aggregate(), "metrics": [{"metric": "nothing"}]}
    as_viewer = ask(server.client("ana"), unknown).json()["error"]
    as_builder = ask(server.client("builder"), unknown).json()["error"]
    assert as_viewer["code"] == as_builder["code"] == "MEMBER_NOT_FOUND"
    assert as_viewer["semantic"]["audience"] == "other"
    assert as_builder["semantic"]["audience"] == "builder"


def test_a_mistyped_attribute_gives_zero_rows_never_all(server: Server) -> None:
    ana = server.author("ana", "finance")
    server.attributes.set(server.builder, "project-1", ana, "tariff", "integer", "5")
    answer = ask(server.client("ana"), detail(10))
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["rows"] == []  # an integer never equals text: no row


@pytest.fixture
def mapped(tmp_path: Path) -> Server:
    return Server(tmp_path, MAPPED)


def snapshot_of(answer: Any) -> int:  # noqa: ANN401
    (source,) = [s for s in answer.json()["receipt"]["data"] if s["for"] == "dataset"]
    return int(source["snapshot"]["id"])


def test_a_mapping_reads_the_callers_own_user_id(mapped: Server) -> None:
    ana = mapped.author("ana", tariff=None)
    mapped.mapping({ana: ["residential"], "someone-else": ["commercial"]})
    run(mapped.folder)
    answer = ask(mapped.client("ana"), detail(10))
    assert answer.status_code == 200, answer.text
    assert [row[0] for row in answer.json()["result"]["rows"]] == [1, 2]


def test_a_revoked_mapping_refuses_the_next_page(mapped: Server) -> None:
    """SEC-REVOKE-001 over HTTP: the mapping that granted the next page's rows is removed
    between the two pages; the cursor is refused and no row of the next page is returned."""
    ana = mapped.author("ana", tariff=None)
    mapped.mapping({ana: ["residential", "commercial"]})
    run(mapped.folder)
    http = mapped.client("ana")
    first = ask(http, detail(2))
    assert [row[0] for row in first.json()["result"]["rows"]] == [1, 2]
    after = first.json()["result"]["page"]["next"]
    mapped.mapping({ana: ["residential"]})
    run(mapped.folder)
    second = ask(http, detail(2, after))
    assert (second.status_code, second.json()["error"]["code"]) == (400, "PAGE_CURSOR_INVALID")
    assert "result" not in second.json()
    again = ask(http, detail(2))  # starting again: the permissions in force now
    assert [row[0] for row in again.json()["result"]["rows"]] == [1, 2]
    assert again.json()["result"]["page"].get("next") is None


def test_a_policy_changed_between_pages_refuses_the_next_page(mapped: Server) -> None:
    ana = mapped.author("ana", tariff=None)
    mapped.mapping({ana: ["residential", "commercial"]})
    run(mapped.folder)
    http = mapped.client("ana")
    after = ask(http, detail(2)).json()["result"]["page"]["next"]
    write(mapped.folder, {"security.yaml": MAPPED + "    exempt_groups: [finance]\n"})
    run(mapped.folder)
    second = ask(http, detail(2, after))
    assert second.json()["error"]["code"] == "PAGE_CURSOR_INVALID"


def test_the_context_names_the_snapshot_the_query_read(mapped: Server) -> None:
    """AC6: the mapping snapshot in the fingerprint is the one the rows were read at, for the
    first page and for the next. The Builder's receipt names the snapshot read."""
    mapped.mapping({mapped.builder: ["residential", "commercial"]})
    run(mapped.folder)
    http = mapped.client("builder")
    first = ask(http, detail(2))
    second = ask(http, detail(2, first.json()["result"]["page"]["next"]))
    assert second.status_code == 200, second.text
    assert [row[0] for row in second.json()["result"]["rows"]] == [3, 4]
    (_, at_first), (_, at_second) = mapped.contexts[-2:]
    assert at_first == snapshot_of(first) == at_second == snapshot_of(second)
