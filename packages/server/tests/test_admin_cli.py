"""#446 AC9 and precision 1: the administration CLI, against a server-mode instance over HTTP,
with no UI. End to end on PostgreSQL: `admin create` on the machine, the forced password change,
a user created, the first Builder assigned, who changes their own temporary password and adds a
group; each step in the audit log. Every command signs in as a person and out at the end; the
password never comes from an argument; plain HTTP is refused except to loopback."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from orvex.access.audit import open_audit_log
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.directory import Directory
from orvex.access.directory.attributes import Attributes
from orvex.access.directory.reads import DirectoryReads
from orvex.server.access_store import access_store, postgres_variables
from orvex.server.admin_cli import remote
from orvex.server.admin_client import Answer, Transport, checked_server
from orvex.server.admin_http import Administration
from orvex.server.audit_settings import audit_settings
from orvex.server.cli import command_parser, main
from orvex.server.identity import load_identity
from orvex.server.instance import home_variable
from orvex.server.server_app import create_server_app
from orvex.server.versions import Versions

from tests.support.access_directory import access_role, audit_events, postgres_connection

IDENTITY = load_identity()
ORIGIN = "https://bi.example.com"
NEW = "a long new passphrase"
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))


def through(app: FastAPI) -> Callable[[str], Transport]:
    """The CLI's transport into `app`, keeping no cookie of its own."""

    def factory(origin: str) -> Transport:
        http = TestClient(app, base_url=origin)

        def send(method: str, path: str, body: bytes | None, headers: dict[str, str]) -> Answer:
            http.cookies.clear()
            answer = http.request(method, path, content=body, headers=headers)
            found = {name: answer.headers.get_list(name) for name in answer.headers}
            return Answer(answer.status_code, found, answer.content)

        return send

    return factory


class Typed:
    """What the person types at each password prompt, in order."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


class Instance:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        assert main(["admin", "create", "--user-name", "root", "--display-name", "Root"]) == 0

    def compose(self) -> None:
        store = access_store("server", IDENTITY, self.folder, os.environ)
        settings_of = audit_settings(IDENTITY, self.folder, os.environ)
        audit = open_audit_log(store, settings_of.key, settings_of.anchors)
        self.store, self.audit = store, audit
        self.sign_in = SignIn(store, audit)
        admin = store_admin(store)
        Settings(store, audit).change(admin, public_origin=ORIGIN)  # #156 will configure this
        administration = Administration(
            Directory(store, audit),
            DirectoryReads(store, audit),
            self.sign_in,
            Attributes(store, audit),
            None,
            frozenset({"project-1"}),
            audit,
        )
        self.app = create_server_app(
            VERSIONS, self.sign_in, Settings(store, audit), IDENTITY.slug,
            administration=administration,
        )  # fmt: skip

    def run(self, typed: Typed, *argv: str) -> tuple[int, str, str]:
        command = command_parser(IDENTITY).parse_args(["admin", *argv])
        out, err = io.StringIO(), io.StringIO()
        code = remote(command, IDENTITY, out, err, {}, typed, through(self.app))
        return code, out.getvalue(), err.getvalue()


def store_admin(store: Any) -> str:  # noqa: ANN401
    with store.transaction(read_only=True) as transaction:
        rows = transaction.rows("SELECT id FROM users WHERE administrator = :yes", yes=True)
    return str(rows[0][0])


@pytest.fixture
def instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[tuple[Instance, str]]:
    monkeypatch.setenv(home_variable(IDENTITY), str(tmp_path))
    with access_role(*postgres_connection()) as (connection, password):
        variable, password_variable = postgres_variables(IDENTITY)
        monkeypatch.setenv(variable, connection)
        monkeypatch.setenv(password_variable, password)
        made = Instance(tmp_path)
        out = capsys.readouterr().out
        temporary = out.split("first sign-in): ")[1].strip()
        made.compose()
        yield made, temporary
        made.store.close()


def kinds(made: Instance) -> list[str]:
    return [e["kind"] for e in audit_events(made.store)]


def test_from_admin_create_to_a_builders_group_with_no_ui(instance: tuple[Instance, str]) -> None:
    made, temporary = instance
    at = ["--server", ORIGIN, "--as"]

    code, _, err = made.run(Typed(temporary), "users", *at, "root", "list")
    assert (code, err.strip()) == (1, "PASSWORD_CHANGE_REQUIRED: Change the temporary password "
                                     "first: run the `admin password` command.")  # fmt: skip
    code, out, _ = made.run(Typed(temporary, temporary, NEW, NEW), "password", *at, "root")
    assert (code, out.strip()) == (0, "password changed")

    code, out, _ = made.run(Typed(NEW), "users", *at, "root", "--json", "create",
                            "--user-name", "bea", "--display-name", "Bea")  # fmt: skip
    assert code == 0
    created = json.loads(out)
    bea, bea_temporary = created["user"]["id"], created["temporary_password"]
    code, _, _ = made.run(Typed(NEW), "members", *at, "root", "set", "project-1", bea, "builder")
    assert code == 0

    code, out, _ = made.run(Typed(bea_temporary, bea_temporary, NEW, NEW), "password", *at, "bea")
    assert code == 0
    code, _, _ = made.run(Typed(NEW), "groups", *at, "bea", "create", "project-1", "finance")
    assert code == 0
    code, out, _ = made.run(Typed(NEW), "groups", *at, "bea", "list", "project-1")
    assert (code, out.strip()) == (0, "finance:")

    seen = kinds(made)
    for kind in ("user.created", "user.password_changed", "membership.added", "group.created"):
        assert kind in seen, kind
    assert seen.count("auth.logout") >= 6  # every command signed out
    assert temporary not in str(audit_events(made.store))
    assert bea_temporary not in str(audit_events(made.store))


def test_a_refusal_exits_1_with_its_code_and_still_signs_out(
    instance: tuple[Instance, str],
) -> None:
    made, temporary = instance
    at = ["--server", ORIGIN, "--as"]
    made.run(Typed(temporary, temporary, NEW, NEW), "password", *at, "root")
    before = kinds(made).count("auth.logout")
    code, out, err = made.run(Typed(NEW), "users", *at, "root", "show", "u-nobody")
    assert (code, out, err.strip()) == (1, "", "USER_NOT_FOUND: No such user.")
    assert kinds(made).count("auth.logout") == before + 1
    code, _, err = made.run(Typed("wrong password"), "users", *at, "root", "list")
    assert (code, err.split(":")[0]) == (1, "LOGIN_FAILED")


def test_the_new_passwords_must_match(instance: tuple[Instance, str]) -> None:
    made, temporary = instance
    code, _, err = made.run(Typed(temporary, temporary, NEW, "other"), "password", "--server",
                            ORIGIN, "--as", "root")  # fmt: skip
    assert (code, err.split(":")[0]) == (1, "VALIDATION_FAILED")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://bi.example.com", True),
        ("http://127.0.0.1:8080", True),
        ("http://localhost:8080", True),
        ("http://[::1]:8080", True),
        ("http://bi.example.com", False),
        ("ftp://bi.example.com", False),
        ("https://bi.example.com/path", False),
        ("", False),
    ],
)
def test_plain_http_is_refused_except_to_loopback(url: str, ok: bool) -> None:
    if ok:
        assert checked_server(url) == url
    else:
        with pytest.raises(ValueError, match=r"server|http"):
            checked_server(url)


def test_the_password_is_never_an_argument() -> None:
    parser = command_parser(IDENTITY)
    for argv in (["admin", "password", "--as", "root", "--password", "x"],
                 ["admin", "users", "--as", "root", "list", "--password", "x"]):  # fmt: skip
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
