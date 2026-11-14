"""The administration CLI's commands (#446): `<slug> admin users|lookup|members|groups|
attributes|identities|password …`, one command per administration route, run over HTTP by
`admin_client` as the signed-in person.

- **Reaching the server.** `--server` (or the `<SLUG>_SERVER` environment variable), and `--as`
  for the user name that signs in. The password is read from the terminal, without echo.
- **Output.** Plain text, or the route's document with `--json`. Exit `0`, or `1` with
  `CODE: message` on standard error. A temporary password is printed once, on standard output.
- **`admin password`** changes the signed-in user's own password, a temporary one included:
  the current one, then the new one twice.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, TextIO
from urllib.parse import quote

from orvex.server.admin_client import (
    Client,
    Refused,
    Transport,
    checked_server,
    network,
    signed_in,
)
from orvex.server.identity import Identity

ADMIN = "/api/v1/admin"
REMOTE = frozenset({"users", "lookup", "members", "groups", "attributes", "identities", "password"})


def _p(value: str) -> str:
    return quote(value, safe="")


def add_remote_parsers(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The commands above, under `admin`."""

    def command(name: str, help: str) -> argparse.ArgumentParser:
        made = actions.add_parser(name, help=help)
        made.add_argument("--server", help="the instance's origin, such as https://bi.example.com")
        made.add_argument("--as", dest="signer", required=True, help="the user name to sign in as")
        made.add_argument("--json", action="store_true", help="print the route's JSON document")
        return made

    def verbs(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any]:
        return parser.add_subparsers(dest="verb", required=True)

    users = verbs(command("users", "the instance's users (administrator)"))
    users.add_parser("list")
    users.add_parser("show").add_argument("user")
    created = users.add_parser("create")
    created.add_argument("--user-name", required=True)
    created.add_argument("--display-name", required=True)
    for verb in ("deactivate", "reactivate", "reset-password"):
        users.add_parser(verb).add_argument("user")
    flag = users.add_parser("administrator")
    flag.add_argument("user")
    flag.add_argument("value", choices=["on", "off"])

    command("lookup", "find a user by exact user name").add_argument("user_name")

    members = verbs(command("members", "a project's members"))
    members.add_parser("list").add_argument("project")
    assigned = members.add_parser("set")
    assigned.add_argument("project")
    assigned.add_argument("user")
    assigned.add_argument("role", choices=["builder", "author", "viewer"])
    removed = members.add_parser("remove")
    removed.add_argument("project")
    removed.add_argument("user")

    groups = verbs(command("groups", "a project's groups (its Builder)"))
    groups.add_parser("list").add_argument("project")
    for verb in ("create", "delete"):
        named = groups.add_parser(verb)
        named.add_argument("project")
        named.add_argument("group")
    for verb in ("add", "remove"):
        member = groups.add_parser(verb)
        member.add_argument("project")
        member.add_argument("group")
        member.add_argument("user")

    attributes = verbs(command("attributes", "a member's attributes (the project's Builder)"))
    listed = attributes.add_parser("list")
    listed.add_argument("project")
    listed.add_argument("user")
    setting = attributes.add_parser("set")
    setting.add_argument("project")
    setting.add_argument("user")
    setting.add_argument("name")
    setting.add_argument("type", choices=["text", "integer", "boolean", "list"])
    setting.add_argument("value", nargs="+", help="one value; several for a list")
    unset = attributes.add_parser("remove")
    unset.add_argument("project")
    unset.add_argument("user")
    unset.add_argument("name")

    identities = verbs(command("identities", "a user's OIDC links (administrator)"))
    identities.add_parser("list").add_argument("user")
    link = identities.add_parser("link")
    link.add_argument("user")
    link.add_argument("provider")
    link.add_argument("subject")
    unlink = identities.add_parser("unlink")
    unlink.add_argument("user")
    unlink.add_argument("link", help="the link's id, from `identities list`")

    command("password", "change your own password, a temporary one included")


def _attribute(kind: str, values: list[str]) -> dict[str, Any]:
    if kind == "list":
        return {"type": "list", "values": values}
    if len(values) != 1:
        raise Refused(422, "VALIDATION_FAILED", "One value, except for a list.")
    if kind == "boolean":
        if values[0] not in {"true", "false"}:
            raise Refused(422, "VALIDATION_FAILED", "A boolean is true or false.")
        return {"type": "boolean", "value": values[0] == "true"}
    return {"type": kind, "value": values[0]}


def _route(c: argparse.Namespace) -> tuple[str, str, dict[str, Any] | None, dict[str, str]]:
    """The command's route: method, path, body and query."""
    what, verb = c.action, getattr(c, "verb", None)
    if what == "lookup":
        return "GET", f"{ADMIN}/user-lookup", None, {"user_name": c.user_name}
    if what == "users":
        user = f"{ADMIN}/users/{_p(c.user)}" if hasattr(c, "user") else ""
        if verb == "list":
            return "GET", f"{ADMIN}/users", None, {}
        if verb == "show":
            return "GET", user, None, {}
        if verb == "create":
            body = {"user_name": c.user_name, "display_name": c.display_name}
            return "POST", f"{ADMIN}/users", body, {}
        if verb == "administrator":
            return "PUT", f"{user}/administrator", {"value": c.value == "on"}, {}
        action = verb if verb in {"deactivate", "reactivate"} else "password-reset"
        return "POST", f"{user}/{action}", {}, {}
    if what == "identities":
        base = f"{ADMIN}/users/{_p(c.user)}/identities"
        if verb == "list":
            return "GET", base, None, {}
        if verb == "link":
            return "POST", base, {"provider": c.provider, "subject": c.subject}, {}
        return "DELETE", f"{base}/{_p(c.link)}", None, {}
    project = f"{ADMIN}/projects/{_p(c.project)}"
    if what == "members":
        if verb == "list":
            return "GET", f"{project}/members", None, {}
        member = f"{project}/members/{_p(c.user)}"
        if verb == "set":
            return "PUT", member, {"role": c.role}, {}
        return "DELETE", member, None, {}
    if what == "groups":
        if verb == "list":
            return "GET", f"{project}/groups", None, {}
        if verb == "create":
            return "POST", f"{project}/groups", {"name": c.group}, {}
        group = f"{project}/groups/{_p(c.group)}"
        if verb == "delete":
            return "DELETE", group, None, {}
        member = f"{group}/members/{_p(c.user)}"
        return ("PUT", member, {}, {}) if verb == "add" else ("DELETE", member, None, {})
    attributes = f"{project}/members/{_p(c.user)}/attributes"
    if verb == "list":
        return "GET", attributes, None, {}
    if verb == "set":
        return "PUT", f"{attributes}/{_p(c.name)}", _attribute(c.type, c.value), {}
    return "DELETE", f"{attributes}/{_p(c.name)}", None, {}


def _text(document: Any) -> list[str]:  # noqa: ANN401 - a route's document
    """The document in plain lines; a temporary password on its own line, once."""
    if document is None:
        return ["done"]
    if "temporary_password" in document:
        lines = [f"user: {document['user']['id']}"] if "user" in document else []
        return [*lines, f"temporary password (shown once): {document['temporary_password']}"]
    if "users" in document:
        return [f"{u['id']}  {u['user_name']}  {u['state']}" for u in document["users"]]
    if "members" in document:
        return [f"{m['id']}  {m['user_name']}  {m['role']}" for m in document["members"]]
    if "groups" in document:
        return [f"{g['name']}: {' '.join(g['members'])}" for g in document["groups"]]
    if "attributes" in document:
        return [json.dumps(a, ensure_ascii=False) for a in document["attributes"]]
    if "identities" in document:
        return [f"{i['id']}  {i['provider']}  {i['subject']}" for i in document["identities"]]
    return [json.dumps(document, ensure_ascii=False)]


def remote(
    command: argparse.Namespace,
    identity: Identity,
    out: TextIO,
    err: TextIO,
    environ: Mapping[str, str] = os.environ,
    ask: Callable[[str], str] = getpass.getpass,
    transport: Callable[[str], Transport] = network,
) -> int:
    """Run one remote command; `ask` reads a password without echo."""
    server = command.server or environ.get(f"{identity.slug.upper()}_SERVER", "")
    try:
        origin = checked_server(server)
    except ValueError as error:
        print(f"VALIDATION_FAILED: {error}", file=err)
        return 1
    try:
        with signed_in(origin, transport(origin), command.signer, lambda: ask("password: ")) as c:
            if command.action == "password":
                return _password(c, ask, out, err)
            method, path, body, query = _route(command)
            document = c.request(method, path, body, **query)
    except Refused as refused:
        print(f"{refused.code}: {refused.message}", file=err)
        return 1
    if command.json:
        print(json.dumps(document if document is not None else {}, ensure_ascii=False), file=out)
    else:
        for line in _text(document):
            print(line, file=out)
    return 0


def _password(c: Client, ask: Callable[[str], str], out: TextIO, err: TextIO) -> int:
    current = ask("current password: ")
    new = ask("new password: ")
    if ask("new password again: ") != new:
        print("VALIDATION_FAILED: the two new passwords differ", file=err)
        return 1
    c.change_password(current, new)
    print("password changed", file=out)
    return 0
