"""A copy of a lock file with one hash changed (#429): CI installs it to show that the installer
refuses a file whose hash does not match.

    python -m tools.ci.tamper pylock.toml /tmp/pylock.tampered.toml

The first `sha256` digest of the lock has its first digit changed; nothing else differs. pip
reads a lock file only under a `pylock.*.toml` name, so the output path keeps one.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

_DIGEST = re.compile(r'(sha256 = ")([0-9a-f])')


def tampered(text: str) -> str:
    found = _DIGEST.search(text)
    if found is None:
        raise SystemExit("the lock file holds no sha256 hash")
    digit = "1" if found.group(2) == "0" else "0"
    return text[: found.start(2)] + digit + text[found.end(2) :]


def main(argv: Sequence[str] | None = None) -> int:
    source, target = list(sys.argv[1:] if argv is None else argv)
    if not re.fullmatch(r"pylock(\..+)?\.toml", Path(target).name):
        raise SystemExit(f"{target}: pip reads a lock file only as pylock.toml or pylock.*.toml")
    Path(target).write_text(tampered(Path(source).read_text("utf-8")), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
