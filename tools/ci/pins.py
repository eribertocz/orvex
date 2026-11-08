"""Print `name==version` for the named packages of a lock file (#376): the Windows test job
installs the lock's versions from their Windows wheels, since the lock holds Linux ones only.

    python -m tools.ci.pins pylock.toml pytest duckdb
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path


def pins(lock: Path, names: Sequence[str]) -> list[str]:
    versions = {p["name"]: p["version"] for p in tomllib.loads(lock.read_text("utf-8"))["packages"]}
    missing = [name for name in names if name not in versions]
    if missing:
        raise SystemExit(f"{lock}: not in the lock: {', '.join(missing)}")
    return [f"{name}=={versions[name]}" for name in names]


def main(argv: Sequence[str] | None = None) -> int:
    lock, *names = list(sys.argv[1:] if argv is None else argv)
    print(" ".join(pins(Path(lock), names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
