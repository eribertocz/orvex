"""The `<slug> admin create` command: create, or recover, a server instance's first
administrator, on the server machine (#436).

It runs only with direct access to the instance: no HTTP route does this. Its store is server
mode's PostgreSQL (`access_store.py`), so it is refused when that is not configured. The first
run writes the instance's recovery file, owner-only, and says where it is; keep it offline.
`--recover` needs that file. Until the audit log exists (#437), every use is recorded as one line
on standard error.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TextIO

from orvex.access.directory import Bootstrap, DirectoryEvent
from orvex.access.errors import AccessError
from orvex.server.access_store import access_store
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


def admin(command: argparse.Namespace, identity: Identity, out: TextIO, err: TextIO) -> int:
    def audit(event: DirectoryEvent) -> None:
        detail = f" code={event.code}" if event.code else f" user={event.target}"
        print(f"audit: {event.operation} {event.outcome}{detail}", file=err)

    instance = instance_directory(identity)
    recovery = instance / RECOVERY_FILE
    try:
        store = access_store("server", identity, instance, os.environ)
    except AccessError as error:
        print(f"{error.code}: {error.message}", file=err)
        return 1
    try:
        bootstrap = Bootstrap(store, recovery, audit)
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
