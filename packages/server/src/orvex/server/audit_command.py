"""The `<slug> admin audit` commands, on the server machine: verify the log, apply its retention,
rotate its signing key (#437).

- `verify [--anchors FILE]` checks the chain, every signature and, when given, an anchor file
  shipped outside: any line that no longer matches the log is reported.
- `retain` removes the events older than the retention setting, up to a signed checkpoint.
- `rotate-key` makes a new key, records the rotation, and only then replaces the key file. If
  the replacement fails, the new key is left beside the old one as `<key>.new` and the log
  refuses to write with the old one until it is moved into place. A running server keeps the
  key it loaded, so it refuses to write after a rotation until it is restarted.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TextIO

from orvex.access.audit import AuditLog
from orvex.access.audit import signing_key as keys
from orvex.access.errors import AccessError


def add_audit_parser(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    audit = actions.add_parser("audit", help="verify, retain or rotate the audit log's key")
    steps = audit.add_subparsers(dest="audit_action", required=True)
    verify = steps.add_parser("verify", help="check the audit log's chain and signatures")
    verify.add_argument("--anchors", type=Path, default=None, help="an anchor file shipped outside")
    steps.add_parser("retain", help="remove events older than the retention setting")
    steps.add_parser("rotate-key", help="replace the audit signing key")


def audit(
    command: argparse.Namespace,
    log: AuditLog,
    key: Path,
    retention_days: int,
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        if command.audit_action == "verify":
            lines = command.anchors.read_text("ascii").splitlines() if command.anchors else []
            print(f"audit log verified: {log.verify(lines)} events", file=out)
        elif command.audit_action == "retain":
            removed = log.retain(retention_days)
            if removed is None:
                print("audit retention: nothing to remove", file=out)
            else:
                print(f"audit retention: removed events {removed[0]} to {removed[1]}", file=out)
        else:
            staged = key.with_name(key.name + ".new")
            if staged.exists() or staged.is_symlink():
                print(
                    f"AUDIT_KEY_UNSAFE: {staged} exists: a rotation was interrupted. Move it into "
                    "place if the log already uses it (verify says which), or remove it.",
                    file=err,
                )
                return 1
            new = keys.create(staged)
            log.rotate(new)
            os.replace(staged, key)
            print(f"audit signing key rotated: {new.fingerprint}", file=out)
    except AccessError as error:
        detail = " ".join(f"{name}={value}" for name, value in sorted(error.details.items()))
        print(f"{error.code}: {error.message} {detail}".rstrip(), file=err)
        return 1
    return 0
