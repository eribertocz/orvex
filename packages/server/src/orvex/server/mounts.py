"""Whether a path is on a local disk (ADR-0027 §2), from Linux's `/proc/self/mountinfo`: the
rule for file catalogs, shared by the managed lake (`lake_place`) and existing lakes
(`project`), moved out of `lake_place.py` unchanged (#399)."""

from __future__ import annotations

import re
from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")
# File systems where file locking cannot be trusted, or that are remote: a DuckDB or SQLite
# catalog there could be corrupted by two writers (ADR-0027 §2). Every FUSE file system
# (`fuse.sshfs`, `fuse.s3fs`, …) is refused too: most are remote, and none can be proven local.
NETWORK_FILE_SYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "afs",
        # Also remote or shared with another system: WebDAV, and folders a virtual machine
        # shares with its host.
        "fuse",
        "davfs",
        "vboxsf",
        "virtiofs",
    }
)


def file_system(path: Path) -> str | None:
    """The type of the file system holding `path` (its deepest existing folder), from Linux's
    `/proc/self/mountinfo`; None where that cannot be read (other platforms)."""
    try:
        mountinfo = MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        return mount_type(existing.resolve(), mountinfo)
    except OSError:
        return None


def mount_type(target: Path, mountinfo: str) -> str | None:
    """The file-system type of the deepest mount point holding `target`, in mountinfo's text;
    a later mount on the same point hides an earlier one."""
    best: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split(" ")
        kind = after.split(" ")[0] if separator else ""
        if len(fields) < 5 or not kind:
            continue
        point = Path(_unescape(fields[4]))
        if target == point or point in target.parents:
            depth = len(point.parts)
            if best is None or depth >= best[0]:
                best = (depth, kind)
    return best[1] if best is not None else None


def _unescape(text: str) -> str:
    """mountinfo writes a space, tab, newline and backslash in a path as octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)
