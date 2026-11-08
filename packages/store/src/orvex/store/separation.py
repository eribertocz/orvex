"""An existing DuckLake kept apart from every other file the instance reads (P2-30 PR 2, #399;
ADR-0027 §3, "No links of any kind" and "Separate locations").

The lake's catalog is untrusted: it decides which files each table reads, and DuckLake allows
absolute paths. So a lake must never reach a file of the **protected set**: the instance folders
and, for every registered project, its folder, its managed lake's data location and catalog
file, its other existing lakes' locations and catalog files, and every path its file sources
read (the server builds the set). Three rules, each failing closed with `STORE_SOURCE_INVALID`:

1. **Locations apart.** The lake's data location and its SQLite catalog neither equal, contain
   nor lie inside a protected location. Paths are compared by whole components after resolving
   them; object storage by bucket and key, whatever the scheme (one store can be reached through
   two spellings of an endpoint, or through another service's interface, so the endpoint never
   tells two stores apart). An Azure location whose first segment names an account
   (`az://<account>.blob.core.windows.net/<container>/…`, the same container as
   `az://<container>/…`) and any location that cannot be compared are refused.
2. **File systems apart.** A hard link cannot cross file systems, and no path check can see one.
   So a local data location, and a SQLite catalog with the `-wal`, `-shm` and `-journal` files
   SQLite opens by itself, must be on a file system that holds no protected location. The file
   systems under a location are its own and those of every mount below it, from Linux's
   `/proc/self/mountinfo`. A device tells file systems apart only where it is the file system's
   own: on a file system that stacks layers or is reached through another (overlay, NFS, CIFS,
   9p, virtiofs, FUSE), a file's device is not that of the disk it is on, and a hard link made in
   a lower layer or on the server passes any device comparison. So the rule holds only when the
   data folder, the SQLite catalog and every protected location are on a type of
   `LOCAL_FILE_SYSTEMS` showing its mount's device; any other type, btrfs (whose subvolumes show
   devices of their own) included, or an unreadable mount table, is refused.
3. **The files of a snapshot.** Every data and delete file a snapshot references lies under the
   data location. A local one is opened from the data folder one component at a time with
   `O_NOFOLLOW`, so a symbolic link anywhere on its path is refused, and must be a regular file on
   the location's file system and none of the protected files.

When a local file is read later, DuckDB's allow-list holds the line: measured on DuckDB 2.0.0, a
data file swapped for a symbolic link out of the location after verification is refused at read.
For object storage it does not: DuckDB checks the address as written, then resolves `..` and
signs the request with the lake's credential. There the boundary at read is the credential, which
must be the lake's own (`check_credentials`).
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path

from orvex.store.errors import StoreError
from orvex.store.locations import azure_pairs

OBJECT_STORAGE = ("s3://", "gs://", "az://")
MOUNTINFO = Path("/proc/self/mountinfo")
# Local file systems that neither stack layers nor reach another machine, where a file's device
# is its disk's: the only ones where no hard link can reach a file on another device.
LOCAL_FILE_SYSTEMS = frozenset({"ext2", "ext3", "ext4", "xfs", "tmpfs"})
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)

type Identity = tuple[int, int]
type Mount = tuple[int, Path, str]  # device, mount point, file-system type


def refused(name: str, reason: str) -> StoreError:
    return StoreError(
        "STORE_SOURCE_INVALID",
        f"Connection {name}: the lake cannot be shown apart from the instance's other files: "
        f"{reason}.",
        {"connection": name, "reason": reason},
    )


def is_object_storage(location: str) -> bool:
    return location.startswith(OBJECT_STORAGE)


def components(name: str, location: str) -> tuple[str, ...]:
    """The parts two locations are compared by: `object`, the bucket and each key segment; or
    `local` and the resolved path's parts. Anything else cannot be compared and is refused."""
    plain(name, location)
    for scheme in OBJECT_STORAGE:
        if location.startswith(scheme):
            parts = location[len(scheme) :].split("/")
            if parts[-1] == "":
                parts.pop()
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise refused(name, "an object-storage location has an empty, . or .. segment")
            if scheme == "az://" and "." in parts[0]:
                # A container name has no dot: this one names an account's endpoint.
                raise refused(name, "an Azure location names an account instead of a container")
            return ("object", *parts)
    if _URL.match(location) or not os.path.isabs(location):
        raise refused(name, "a location is neither an absolute path nor object storage")
    return ("local", *Path(os.path.realpath(location)).parts)


def plain(name: str, path: str) -> str:
    """`path`, refused if it holds a NUL character (no file system path can)."""
    if "\x00" in path:
        raise refused(name, "a location holds a NUL character")
    return path


def overlaps(name: str, first: str, second: str) -> bool:
    """Whether one location equals, contains or lies inside the other."""
    a, b = components(name, first), components(name, second)
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def check_apart(
    name: str,
    data_path: str,
    catalog: str | None,
    protected: Sequence[str],
    protected_files: Sequence[str] = (),
) -> None:
    """Rules 1 and 2 for a lake whose data is at `data_path` and whose SQLite catalog (if any)
    is `catalog`, against the protected set as it stands now."""
    components(name, data_path)
    if catalog is not None:
        components(name, catalog)
    for path in protected_files:
        plain(name, path)
    for location in protected:
        if overlaps(name, data_path, location):
            raise refused(name, "its data location overlaps a protected location")
        if catalog is not None and overlaps(name, catalog, location):
            raise refused(name, "its SQLite catalog lies in a protected location")
    local_data = not is_object_storage(data_path)
    if not local_data and catalog is None:
        return
    table = _mounts(name)
    devices: set[int] = set()
    for location in (*protected, *protected_files):
        if not is_object_storage(location):
            devices |= _devices_under(name, location, table)
    for path in protected_files:
        for suffix in ("-wal", "-shm", "-journal", ".wal"):
            devices |= _device_if_present(name, path + suffix)
    if local_data and own_device(name, data_path, table) in devices:
        raise refused(
            name,
            "its data location is on a file system that holds a protected location, so a hard "
            "link there could reach a protected file",
        )
    if catalog is not None:
        holding = _holding(Path(os.path.realpath(catalog)), table)
        if holding is None or holding[2] not in LOCAL_FILE_SYSTEMS:
            raise refused(name, f"its SQLite catalog is on {_kind(holding)}{_LOCAL_ONLY}")
        for path in (catalog, *(catalog + suffix for suffix in ("-wal", "-shm", "-journal"))):
            found = _device_if_present(name, path)
            if path == catalog and not found:
                raise refused(name, "its SQLite catalog does not exist")
            if found != {holding[0]} and found:
                raise refused(name, "its SQLite catalog's file system cannot be identified")
            if found & devices:
                raise refused(
                    name,
                    "its SQLite catalog is on a file system that holds a protected location, so "
                    "a hard link there could reach a protected file",
                )


_LOCAL_ONLY = (
    f", where a device does not tell file systems apart (accepted: "
    f"{', '.join(sorted(LOCAL_FILE_SYSTEMS))})"
)


def own_device(name: str, folder: str, table: list[Mount] | None = None) -> int:
    """The device of a lake's local data folder, which must exist, be on a type of
    `LOCAL_FILE_SYSTEMS` and show its mount's device."""
    table = _mounts(name) if table is None else table
    try:
        status = os.stat(plain(name, folder))
    except OSError:
        raise refused(name, "its data location cannot be examined") from None
    if not stat.S_ISDIR(status.st_mode):
        raise refused(name, "its data location is not a folder")
    holding = _holding(Path(os.path.realpath(folder)), table)
    if holding is None or holding[2] not in LOCAL_FILE_SYSTEMS:
        raise refused(name, f"its data location is on {_kind(holding)}{_LOCAL_ONLY}")
    if holding[0] != status.st_dev:
        raise refused(
            name, "its data location's file system cannot be identified from the mount table"
        )
    return status.st_dev


def _holding(path: Path, table: list[Mount]) -> Mount | None:
    """The mount that holds `path`: the deepest, and of several on one point the latest."""
    found = [mount for mount in table if _within(path, mount[1])]
    deepest = max((len(mount[1].parts) for mount in found), default=-1)
    latest = [mount for mount in found if len(mount[1].parts) == deepest]
    return latest[-1] if latest else None


def _kind(mount: Mount | None) -> str:
    return "a file system the mount table does not list" if mount is None else mount[2]


def check_files(
    name: str,
    data_path: str,
    paths: Iterable[str],
    folder: Identity | None,
    protected: frozenset[Identity],
) -> None:
    """Rule 3 for the files of one snapshot. `folder` is the data folder's device and inode as
    it was attached (None for object storage); `protected` the protected files' identities."""
    prefix = data_path if data_path.endswith("/") else data_path + "/"
    relative: list[list[str]] = []
    for path in paths:
        plain(name, path)
        if not path.startswith(prefix):
            raise refused(name, "a snapshot references a file outside its data location")
        parts = path[len(prefix) :].split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise refused(name, "a snapshot references a file by an ambiguous path")
        relative.append(parts)
    if folder is None or not relative:
        return
    opened: dict[tuple[str, ...], int] = {}
    try:
        root = _open(name, os.path.realpath(prefix), _FOLDER, None)
        opened[()] = root
        status = os.fstat(root)
        if (status.st_dev, status.st_ino) != folder:
            raise refused(name, "its data folder was replaced after it was attached")
        for parts in relative:
            parent = root
            for depth in range(1, len(parts)):
                key = tuple(parts[:depth])
                if key not in opened:
                    opened[key] = _open(name, parts[depth - 1], _FOLDER, parent)
                parent = opened[key]
            descriptor = _open(name, parts[-1], _FILE, parent)
            try:
                status = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise refused(name, "a snapshot references something that is not a regular file")
            if status.st_dev != folder[0]:
                raise refused(name, "a snapshot references a file on another file system")
            if (status.st_dev, status.st_ino) in protected:
                raise refused(name, "a snapshot references a hard link to a protected file")
    finally:
        for descriptor in opened.values():
            with contextlib.suppress(OSError):
                os.close(descriptor)


def folder_identity(name: str, data_path: str) -> Identity | None:
    """The data folder's device and inode as it is now (None for object storage)."""
    if is_object_storage(data_path):
        return None
    descriptor = _open(name, os.path.realpath(data_path), _FOLDER, None)
    try:
        status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return (status.st_dev, status.st_ino)


def _open(name: str, path: str, flags: int, folder: int | None) -> int:
    """A descriptor of `path` itself (relative to `folder`), never of a link's target."""
    try:
        return os.open(path, flags, dir_fd=folder)
    except OSError as error:
        if error.errno == errno.ELOOP or (error.errno == errno.ENOTDIR and _is_link(path, folder)):
            reason = "a snapshot's file is reached through a symbolic link"
        elif error.errno == errno.ENOTDIR:
            reason = "a snapshot's file is reached through something that is not a folder"
        elif error.errno == errno.ENOENT:
            reason = "a snapshot references a file that does not exist"
        else:
            reason = f"a snapshot's file cannot be opened ({errno.errorcode.get(error.errno or 0)})"
        raise refused(name, reason) from None


def _is_link(path: str, folder: int | None) -> bool:
    """Whether `path` (relative to `folder`) is a symbolic link: `O_DIRECTORY` with
    `O_NOFOLLOW` reports one as `ENOTDIR` on Linux."""
    try:
        return stat.S_ISLNK(os.stat(path, dir_fd=folder, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _mounts(name: str) -> list[Mount]:
    """Every mount's device, mount point and type, in the table's order; refused when
    unreadable."""
    try:
        text = MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise refused(name, "the mount table cannot be read on this platform") from None
    return parse_mounts(text)


def parse_mounts(text: str) -> list[Mount]:
    """mountinfo's lines: the device (field 3), the mount point (field 5, octal escapes
    undone) and the type (the first field after ` - `)."""
    found: list[Mount] = []
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split(" ")
        kind = after.split(" ")[0] if separator else ""
        if len(fields) < 5 or ":" not in fields[2] or not kind:
            continue
        major, _, minor = fields[2].partition(":")
        if not (major.isdigit() and minor.isdigit()):
            continue
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
        found.append((os.makedev(int(major), int(minor)), Path(point), kind))
    return found


def _devices_under(name: str, location: str, table: list[Mount]) -> set[int]:
    """The devices of every file that is or may come to be at or under `location`: that of its
    deepest existing part, and those of every mount at or below it. Each of those mounts must be
    of a type of `LOCAL_FILE_SYSTEMS`, where the device is the file system's own."""
    resolved = Path(os.path.realpath(plain(name, location)))
    existing = resolved
    while True:
        try:
            device = os.stat(existing).st_dev
            break
        except FileNotFoundError:
            if existing.parent == existing:
                raise refused(name, "a protected location cannot be examined") from None
            existing = existing.parent
        except OSError:
            raise refused(name, "a protected location cannot be examined") from None
    below = [mount for mount in table if _within(mount[1], resolved)]
    holding = _holding(existing, table)
    for mount in [holding, *below]:
        if mount is None or mount[2] not in LOCAL_FILE_SYSTEMS:
            raise refused(name, f"a protected location is on {_kind(mount)}{_LOCAL_ONLY}")
    return {device} | {mount[0] for mount in below}


def _device_if_present(name: str, path: str) -> set[int]:
    try:
        return {os.stat(plain(name, path)).st_dev}
    except FileNotFoundError:
        return set()
    except OSError:
        raise refused(name, "a protected file cannot be examined") from None


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def credential_values(text: str) -> set[str]:
    """What identifies a credential held in one variable: the value itself, and an Azure
    connection string's account key or shared access signature."""
    found = {text} if text else set()
    pairs = azure_pairs(text) if "=" in text else None
    for key in ("AccountKey", "SharedAccessSignature"):
        if pairs and pairs.get(key):
            found.add(pairs[key])
    return found


def check_credentials(
    name: str, own: dict[str, str], in_use: Sequence[str] | None, values: dict[str, str]
) -> None:
    """The object-storage credential of a lake must be its own (`own`: its variables and their
    values), as its catalog role must be: DuckDB signs every object-storage request with it,
    `..` resolved after the allow-list was checked, so at read time the credential's own reach
    is the boundary. `in_use` names every other variable of the instance, `values` their values
    where set. A lake without a credential of its own is refused."""
    if in_use is None:
        raise refused(name, "the instance's credentials in use were not given")
    mine = set().union(*(credential_values(value) for value in own.values()))
    if not mine:
        raise refused(name, "its object storage has no credential of its own")
    if set(own) & set(in_use):
        raise refused(name, "its object-storage credential is another connection's variable")
    for variable in in_use:
        if mine & credential_values(values.get(variable, "")):
            raise refused(name, "its object-storage credential is another connection's")
