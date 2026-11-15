"""The `<slug> admin` commands, on the server machine: create, or recover, a server instance's
first administrator (#436), and the audit log's commands (#437). The administration commands
that run over HTTP, from any machine, are `admin_cli`'s (#446).

It runs only with direct access to the instance: no HTTP route does this. Its store is server
mode's PostgreSQL (`access_store.py`), reached through the access contract, so it is refused
when that is not configured. The first run writes the instance's recovery file, owner-only,
and says where it is; keep it offline.
`--recover` needs that file. The new administrator's temporary password (#438) is printed once:
it expires after 24 hours and must be changed at the first sign-in. Every use is recorded in the
audit log (#437), whose signing key the first run also creates; `admin audit` verifies, retains
and rotates it (`audit_command.py`).
"""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path
from typing import TextIO

from orvex.contracts.access.client import AccessClient, system
from orvex.contracts.access.errors import AccessError
from orvex.server.access_configuration import RECOVERY_FILE, access_configuration
from orvex.server.admin_cli import REMOTE, add_remote_parsers, remote
from orvex.server.assembly import access_bindings
from orvex.server.audit_command import add_audit_parser, audit
from orvex.server.audit_settings import audit_settings
from orvex.server.identity import Identity
from orvex.server.instance import instance_directory


def add_admin_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    admin = commands.add_parser("admin", help="administer a server instance")
    actions = admin.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="create the first administrator")
    create.add_argument("--user-name", required=True, help="the administrator's user name")
    create.add_argument("--display-name", required=True, help="the name people see")
    create.add_argument(
        "--recover",
        action="store_true",
        help="create one although administrators exist (needs the recovery file)",
    )
    create.add_argument(
        "--recovery-file",
        type=Path,
        default=None,
        help="the recovery file, when it is not in the instance directory",
    )
    add_audit_parser(actions)
    add_remote_parsers(actions)


def admin(command: argparse.Namespace, identity: Identity, out: TextIO, err: TextIO) -> int:
    if command.action in REMOTE:  # over HTTP, as the signed-in person (#446)
        return remote(command, identity, out, err)
    instance = instance_directory(identity)
    recovery = instance / RECOVERY_FILE
    try:
        settings = audit_settings(identity, instance, os.environ)
        configuration = access_configuration(
            "server", identity, instance, os.environ, settings, with_oidc=False
        )
    except AccessError as error:
        print(f"{error.code}: {error.message}", file=err)
        return 1
    access, log = access_bindings(identity)
    caller = system(uuid.uuid4().hex)
    try:
        opened = AccessClient(access, log, caller).open(configuration)
    except AccessError as error:
        print(f"{error.code}: {error.message}", file=err)
        return 1
    try:
        if command.action == "audit":
            return audit(command, opened, settings.retention_days, out, err)
        if opened.initialize():
            print(f"recovery file written: {recovery} (keep a copy offline)", file=out)
        given = command.recovery_file
        created = opened.create_administrator(
            command.user_name,
            command.display_name,
            recover=command.recover,
            recovery=None if given is None else str(given),
        )
    except AccessError as error:
        print(f"{error.code}: {error.message}", file=err)
        return 1
    finally:
        opened.close()
    print(f"administrator created: {created.user.id}", file=out)
    print(
        f"temporary password (shown once, valid 24 hours, to change at the first sign-in): "
        f"{created.temporary_password}",
        file=out,
    )
    return 0
