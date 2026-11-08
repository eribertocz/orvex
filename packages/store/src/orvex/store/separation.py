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
   tells two stores apart). A location that cannot be compared is refused.
2. **File systems apart.** A hard link cannot cross file systems, and no path check can see one.
   So a local data location, and a SQLite catalog with the `-wal`, `-shm` and `-journal` files
   SQLite opens by itself, must be on a file system that holds no protected location. The file
   systems under a location are its own and those of every mount below it, from Linux's
   `/proc/self/mountinfo`. The lake's own folder must show its mount's device, otherwise (a
   nested subvolume, an overlay, an unreadable mount table) it cannot be compared and is refused.
3. **The files of a snapshot.** Every data and delete file a snapshot references lies under the
   data location. A local one is opened from the data folder one component at a time with
   `O_NOFOLLOW`, so a symbolic link anywhere on its path is refused, and must be a regular file on
   the location's file system and none of the protected files.

When a file is read later, DuckDB's allow-list holds the line: measured on DuckDB 2.0.0, a data
file swapped for a symbolic link out of the location after verification is refused at read.
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

OBJECT_STORAGE = ("s3://", "gs://", "az://")
MOUNTINFO = Path("/proc/self/mountinfo")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)

type Identity = tuple[int, int]


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
    for scheme in OBJECT_STORAGE:
        if location.startswith(scheme):
            parts = location[len(scheme) :].split("/")
            if parts[-1] == "":
                parts.pop()
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise refused(name, "an object-storage location has an empty, . or .. segment")
            return ("object", *parts)
    if _URL.match(location) or not os.path.isabs(location):
        raise refused(name, "a location is neither an absolute path nor object storage")
    return ("local", *Path(os.path.realpath(location)).parts)


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
        for path in (catalog, *(catalog + suffix for suffix in ("-wal", "-shm", "-journal"))):
            found = _device_if_present(name, path)
            if path == catalog and not found:
                raise refused(name, "its SQLite catalog does not exist")
            if found & devices:
                raise refused(
                    name,
                    "its SQLite catalog is on a file system that holds a protected location, so "
                    "a hard link there could reach a protected file",
                )


def own_device(name: str, folder: str, table: list[tuple[int, Path]] | None = None) -> int:
    """The device of a lake's local data folder, which must exist and be its mount's device."""
    table = _mounts(name) if table is None else table
    try:
        status = os.stat(folder)
    except OSError:
        raise refused(name, "its data location cannot be examined") from None
    if not stat.S_ISDIR(status.st_mode):
        raise refused(name, "its data location is not a folder")
    resolved = Path(os.path.realpath(folder))
    holding = [(device, point) for device, point in table if _within(resolved, point)]
    deepest = max((len(point.parts) for _, point in holding), default=-1)
    # The deepest mount holds the folder; of several mounts on one point, the latest.
    latest = [device for device, point in holding if len(point.parts) == deepest]
    if not latest or latest[-1] != status.st_dev:
        raise refused(
            name, "its data location's file system cannot be identified from the mount table"
        )
    return status.st_dev


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


def _mounts(name: str) -> list[tuple[int, Path]]:
    """Every mount's device and mount point, in the table's order; refused when unreadable."""
    try:
        text = MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise refused(name, "the mount table cannot be read on this platform") from None
    return parse_mounts(text)


def parse_mounts(text: str) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 5 or ":" not in fields[2]:
            continue
        major, _, minor = fields[2].partition(":")
        if not (major.isdigit() and minor.isdigit()):
            continue
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
        found.append((os.makedev(int(major), int(minor)), Path(point)))
    return found


def _devices_under(name: str, location: str, table: list[tuple[int, Path]]) -> set[int]:
    """The devices of every file that is or may come to be at or under `location`: that of its
    deepest existing part, and those of every mount at or below it."""
    resolved = Path(os.path.realpath(location))
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
    return {device} | {found for found, point in table if _within(point, resolved)}


def _device_if_present(name: str, path: str) -> set[int]:
    try:
        return {os.stat(path).st_dev}
    except FileNotFoundError:
        return set()
    except OSError:
        raise refused(name, "a protected file cannot be examined") from None


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents
