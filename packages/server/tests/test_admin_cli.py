"""#446 AC9 and precision 1: the administration CLI, against a server-mode instance over HTTP,
with no UI. End to end on PostgreSQL: `admin create` on the machine, the forced password change,
a user created, the first Builder assigned, who changes their own temporary password and adds a
group; each step in the audit log. Every command signs in as a person and out at the end; the
password never comes from an argument; plain HTTP is refused except to loopback."""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from orvex.contracts.access.client import AccessClient, system
from orvex.server.access_configuration import access_configuration
from orvex.server.access_store import postgres_variables
from orvex.server.admin_cli import remote
from orvex.server.admin_client import Answer, Transport, checked_server
from orvex.server.admin_http import Administration
from orvex.server.assembly import access_bindings
from orvex.server.audit_settings import audit_settings
from orvex.server.cli import command_parser, main
from orvex.server.identity import load_identity
from orvex.server.instance import home_variable
from orvex.server.server_app import create_server_app
from orvex.server.versions import Versions

from .access_instance import PASSWORD, postgres_location
from .admin_instance import AdminInstance, admin_instance

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
    def __init__(self, folder: Path, admin: str) -> None:
        self.folder = folder
        self.admin = admin

    def compose(self) -> None:
        settings_of = audit_settings(IDENTITY, self.folder, os.environ)
        configuration = access_configuration(
            "server", IDENTITY, self.folder, os.environ, settings_of, with_oidc=False
        )
        opened = AccessClient(*access_bindings(IDENTITY), system("t")).open(configuration)
        self.access = opened
        opened.as_actor(self.admin).change_sign_in_settings(public_origin=ORIGIN)  # #156
        administration = Administration(opened, frozenset({"project-1"}))
        self.app = create_server_app(VERSIONS, opened, IDENTITY.slug, administration=administration)

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(event) for event in self.access.events()]

    def run(self, typed: Typed, *argv: str) -> tuple[int, str, str]:
        command = command_parser(IDENTITY).parse_args(["admin", *argv])
        out, err = io.StringIO(), io.StringIO()
        code = remote(command, IDENTITY, out, err, {}, typed, through(self.app))
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[tuple[Instance, str]]:
    monkeypatch.setenv(home_variable(IDENTITY), str(tmp_path))
    with postgres_location() as location:
        variable, password_variable = postgres_variables(IDENTITY)
        monkeypatch.setenv(variable, location.connection)
        monkeypatch.setenv(password_variable, location.password)
        created = ["admin", "create", "--user-name", "root", "--display-name", "Root"]
        assert main(created) == 0
        out = capsys.readouterr().out
        admin = out.split("administrator created: ")[1].split()[0]
        temporary = out.split("first sign-in): ")[1].strip()
        made = Instance(tmp_path, admin)
        made.compose()
        yield made, temporary
        made.access.close()


def kinds(made: Instance) -> list[str]:
    return [e["kind"] for e in made.events()]


def test_from_admin_create_to_a_builders_group_with_no_ui(
    instance: tuple[Instance, str],
    caplog: Any,  # noqa: ANN401
) -> None:
    caplog.set_level(logging.DEBUG)
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
    for secret in (temporary, bea_temporary, NEW):
        assert secret not in str(made.events())
        assert secret not in caplog.text


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


@pytest.fixture
def sqlite_instance(tmp_path: Path) -> Iterator[AdminInstance]:
    with admin_instance("sqlite", tmp_path) as made:
        yield made


def test_every_command_runs_its_route(sqlite_instance: AdminInstance, caplog: Any) -> None:  # noqa: ANN401
    """Each command, with its route's outcome: exit 0 and the change made, or exit 1 with the
    route's code; no subject or temporary password in any log line."""
    caplog.set_level(logging.DEBUG)
    made = sqlite_instance
    ana = made.person("ana")
    builder = made.person("bob")
    made.actor(made.admin).set_membership("project-1", builder, "builder")
    app = made.app

    def run(signer: str, *argv: str) -> tuple[int, str, str]:
        command = command_parser(IDENTITY).parse_args(
            ["admin", argv[0], "--server", ORIGIN, "--as", signer, *argv[1:]]
        )
        out, err = io.StringIO(), io.StringIO()
        code = remote(command, IDENTITY, out, err, {}, Typed(PASSWORD), through(app))
        return code, out.getvalue(), err.getvalue()

    def ok(signer: str, *argv: str) -> str:
        code, out, err = run(signer, *argv)
        assert code == 0, (argv, err)
        return out

    assert ok("root", "users", "list").count("\n") == 3
    assert ana in ok("root", "users", "show", ana)
    secret = ok("root", "users", "reset-password", ana).split(": ")[-1].strip()
    assert ok("root", "users", "deactivate", ana).strip() == "done"
    assert made.actor(made.admin).user(ana).state == "deactivated"
    ok("root", "users", "reactivate", ana)
    ok("root", "users", "administrator", ana, "on")
    assert made.actor(made.admin).user(ana).administrator
    ok("root", "users", "administrator", ana, "off")
    assert json.loads(ok("bob", "lookup", "--json", "ANA")) == {"id": ana, "display_name": "Ana"}
    assert run("bob", "lookup", "nobody")[2].startswith("USER_NOT_FOUND")
    link = json.loads(ok("root", "identities", "--json", "link", ana, "corp", "sub-ana"))["id"]
    assert link in ok("root", "identities", "list", ana)
    ok("root", "identities", "unlink", ana, link)
    assert run("root", "identities", "unlink", ana, link)[2].startswith("IDENTITY_NOT_FOUND")
    ok("bob", "members", "set", "project-1", ana, "author")
    assert "author" in ok("bob", "members", "list", "project-1")
    ok("bob", "groups", "create", "project-1", "finance")
    ok("bob", "groups", "add", "project-1", "finance", ana)
    assert ok("bob", "groups", "list", "project-1").strip() == f"finance: {ana}"
    ok("bob", "groups", "remove", "project-1", "finance", ana)
    ok("bob", "groups", "delete", "project-1", "finance")
    ok("bob", "attributes", "set", "project-1", ana, "tariff", "text", "residential")
    ok("bob", "attributes", "set", "project-1", ana, "zones", "list", "n", "s")
    ok("bob", "attributes", "set", "project-1", ana, "trusted", "boolean", "true")
    listed = ok("bob", "attributes", "--json", "list", "project-1", ana)
    assert {a["name"] for a in json.loads(listed)["attributes"]} == {"tariff", "zones", "trusted"}
    ok("bob", "attributes", "remove", "project-1", ana, "tariff")
    ok("bob", "members", "remove", "project-1", ana)
    assert run("bob", "members", "list", "elsewhere")[2].startswith("PROJECT_NOT_SERVED")
    assert run("bob", "users", "list")[2].startswith("ACCESS_FORBIDDEN")
    assert "sub-ana" not in caplog.text
    assert secret not in caplog.text


def test_an_unreachable_server_is_a_refusal_not_a_traceback() -> None:
    def broken(origin: str) -> Transport:
        def send(method: str, path: str, body: bytes | None, headers: dict[str, str]) -> Answer:
            raise ConnectionRefusedError("connection refused")

        return send

    command = command_parser(IDENTITY).parse_args(
        ["admin", "users", "--server", ORIGIN, "--as", "root", "list"]
    )
    err = io.StringIO()
    assert remote(command, IDENTITY, io.StringIO(), err, {}, Typed("x"), broken) == 1
    assert err.getvalue().startswith("SERVER_UNREACHABLE: connection refused")


def test_a_server_url_with_user_information_is_refused() -> None:
    with pytest.raises(ValueError, match="origin"):
        checked_server("https://user:secret@bi.example.com")
