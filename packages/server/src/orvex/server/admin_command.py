"""The `<slug> admin` commands, on the server machine: create, or recover, a server instance's
first administrator (#436), and the audit log's commands (#437).

It runs only with direct access to the instance: no HTTP route does this. Its store is server
mode's PostgreSQL (`access_store.py`), so it is refused when that is not configured. The first
run writes the instance's recovery file, owner-only, and says where it is; keep it offline.
`--recover` needs that file. Every use is recorded in the audit log (#437), whose signing key
the first run also creates; `admin audit` verifies, retains and rotates it (`audit_command.py`).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TextIO

from orvex.access.audit import open_audit_log
from orvex.access.directory import Bootstrap
from orvex.access.errors import AccessError
from orvex.server.access_store import access_store
from orvex.server.audit_command import add_audit_parser, audit
from orvex.server.audit_settings import audit_settings
from orvex.server.identity import Identity
from orvex.server.instance import instance_directory

RECOVERY_FILE = "access-recovery.key"


def add_admin_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    admin = commands.add_parser("admin", help="administer a server instance (on its machine)")
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


def admin(command: argparse.Namespace, identity: Identity, out: TextIO, err: TextIO) -> int:
    instance = instance_directory(identity)
    recovery = instance / RECOVERY_FILE
    try:
        settings = audit_settings(identity, instance, os.environ)
        store = access_store("server", identity, instance, os.environ)
    except AccessError as error:
        print(f"{error.code}: {error.message}", file=err)
        return 1
    try:
        log = open_audit_log(store, settings.key, settings.anchors)
        if command.action == "audit":
            return audit(command, log, settings.key, settings.retention_days, out, err)
        bootstrap = Bootstrap(store, recovery, log)
        if bootstrap.initialize():
            print(f"recovery file written: {recovery} (keep a copy offline)", file=out)
        created = bootstrap.create_administrator(
            command.user_name,
            command.display_name,
            recover=command.recover,
            recovery=command.recovery_file,
        )
    except AccessError as error:
        print(f"{error.code}: {error.message}", file=err)
        return 1
    finally:
        store.close()
    print(f"administrator created: {created.id}", file=out)
    return 0
