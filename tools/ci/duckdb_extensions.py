"""Install the DuckDB extensions the store uses, from DuckDB's official server (#240).

    python -m tools.ci.duckdb_extensions

DuckDB downloads extensions over `http`, which the development sandbox's proxy does not carry.
This tool downloads the same official files over `https` from `extensions.duckdb.org`, for the
DuckDB version pinned in the lock file and the platform DuckDB reports, and installs each with
DuckDB's own `INSTALL '<file>'`. DuckDB then loads it with unsigned extensions refused, so it
verifies the official signature; a file that does not load is removed. Nothing downloaded runs
except through that `LOAD`. Every failure stops the tool (fail closed).
"""

from __future__ import annotations

import ast
import gzip
import sys
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

import duckdb

from tools.repository import REPOSITORY_ROOT

SERVER = "https://extensions.duckdb.org/"
MAX_BYTES = 256 * 1024 * 1024  # the largest official file today is about 60 MB unpacked
STORE = Path("packages/store/src")
USER_AGENT = "duckdb-extension-installer (development tool)"
# DuckDB's own names for the aliases the store uses (`duckdb_extensions().aliases`).
FILE_NAMES = {"sqlite": "sqlite_scanner", "postgres": "postgres_scanner"}

type Fetch = Callable[[str, Path], None]


class ExtensionError(Exception):
    """A failure that stops the installation; the message starts with its code."""


def store_extensions(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Every extension the store installs: the literal names of `EXTENSIONS = (...)` and of
    `_load_extension("...")` calls. Any other form fails, so none can be missed."""
    names: set[str] = set()
    for path in sorted((root / STORE).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # `for name in EXTENSIONS: self._load_extension(name)` passes the literal list's names.
        from_list: set[int] = set()
        for loop in ast.walk(tree):
            if (
                isinstance(loop, ast.For)
                and isinstance(loop.iter, ast.Name)
                and loop.iter.id == "EXTENSIONS"
                and isinstance(loop.target, ast.Name)
            ):
                for inner in ast.walk(loop):
                    if (
                        isinstance(inner, ast.Call)
                        and len(inner.args) == 1
                        and isinstance(inner.args[0], ast.Name)
                        and inner.args[0].id == loop.target.id
                    ):
                        from_list.add(id(inner))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "EXTENSIONS" for t in node.targets)
                and isinstance(node.value, ast.Tuple)
            ):
                for element in node.value.elts:
                    names.add(_literal(element, path))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_load_extension"
            ):
                if id(node) in from_list:
                    continue
                if len(node.args) != 1:
                    raise ExtensionError(f"EXTENSION_NAME_NOT_LITERAL: {path}:{node.lineno}")
                names.add(_literal(node.args[0], path))
    return sorted(names)


def _literal(node: ast.expr, path: Path) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise ExtensionError(f"EXTENSION_NAME_NOT_LITERAL: {path}:{node.lineno}")


def locked_version(root: Path, python: tuple[int, int]) -> str:
    """The DuckDB version pinned in the lock file CI installs for this Python."""
    lock = root / ("pylock.py314.toml" if python == (3, 14) else "pylock.toml")
    packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("packages", [])
    versions = [p.get("version") for p in packages if p.get("name") == "duckdb"]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ExtensionError(f"DUCKDB_VERSION_UNKNOWN: no single duckdb pin in {lock.name}")
    return versions[0]


def check_version(root: Path = REPOSITORY_ROOT) -> str:
    """The installed DuckDB version, refused unless it is the one the lock file pins."""
    pinned = locked_version(root, (sys.version_info.major, sys.version_info.minor))
    if duckdb.__version__ != pinned:
        raise ExtensionError(
            f"DUCKDB_VERSION_MISMATCH: installed {duckdb.__version__}, the lock file pins {pinned}"
        )
    return pinned


def file_name(name: str) -> str:
    return FILE_NAMES.get(name, name)


def copy_bounded(chunks: Iterable[bytes], destination: Path, limit: int = MAX_BYTES) -> None:
    written = 0
    with destination.open("wb") as handle:
        for chunk in chunks:
            written += len(chunk)
            if written > limit:
                handle.close()
                destination.unlink()
                raise ExtensionError(f"EXTENSION_TOO_LARGE: more than {limit} bytes")
            handle.write(chunk)


def download(url: str, destination: Path) -> None:
    """Download `url`, only from the official server over https, at most MAX_BYTES."""
    if not url.startswith(SERVER):
        raise ExtensionError(f"EXTENSION_SOURCE_REFUSED: {url}")
    # The server refuses Python's default user agent (measured: 403).
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.geturl() != url:
            raise ExtensionError(f"EXTENSION_SOURCE_REFUSED: redirected to {response.geturl()}")
        copy_bounded(iter(lambda: response.read(1 << 20), b""), destination)


def _connect(directory: Path | None) -> duckdb.DuckDBPyConnection:
    config: dict[str, str | bool | int | float | list[str]] = {"allow_unsigned_extensions": False}
    if directory is not None:
        config["extension_directory"] = str(directory)
    return duckdb.connect(config=config)


def _installed_file(connection: duckdb.DuckDBPyConnection, name: str) -> Path | None:
    directory = connection.execute("SELECT current_setting('extension_directory')").fetchone()
    platform = connection.execute("SELECT platform FROM pragma_platform()").fetchone()
    base = (
        Path(str(directory[0]))
        if directory and directory[0]
        else Path.home() / ".duckdb" / "extensions"
    )
    path = (
        base
        / f"v{duckdb.__version__}"
        / str(platform[0] if platform else "")
        / (f"{file_name(name)}.duckdb_extension")
    )
    return path if path.is_file() else None


def ensure(
    names: Iterable[str], directory: Path | None = None, fetch: Fetch = download
) -> list[str]:
    """Install each extension that is not installed or does not load; return the ones
    installed. One that is installed and loads is left as it is."""
    installed: list[str] = []
    for name in names:
        connection = _connect(directory)
        try:
            present = _installed_file(connection, name)
            if present is not None:
                try:
                    connection.execute(f"LOAD {name}")
                    continue
                except duckdb.Error:
                    # Installed but not loadable (a damaged or foreign file): replaced below.
                    present.unlink()
                    present.with_name(present.name + ".info").unlink(missing_ok=True)
            platform = connection.execute("SELECT platform FROM pragma_platform()").fetchone()
            url = (
                f"{SERVER}v{duckdb.__version__}/{platform[0] if platform else ''}/"
                f"{file_name(name)}.duckdb_extension.gz"
            )
            with tempfile.TemporaryDirectory() as scratch:
                packed = Path(scratch) / f"{file_name(name)}.duckdb_extension.gz"
                fetch(url, packed)
                unpacked = Path(scratch) / f"{file_name(name)}.duckdb_extension"
                with gzip.open(packed, "rb") as source:
                    copy_bounded(iter(lambda: source.read(1 << 20), b""), unpacked)
                try:
                    connection.execute(f"INSTALL '{unpacked.as_posix()}'")
                    connection.execute(f"LOAD {name}")
                except duckdb.Error as error:
                    leftover = _installed_file(connection, name)
                    if leftover is not None:
                        leftover.unlink()
                        leftover.with_name(leftover.name + ".info").unlink(missing_ok=True)
                    raise ExtensionError(f"EXTENSION_NOT_VERIFIED: {name}: {error}") from None
            installed.append(name)
        finally:
            connection.close()
    return installed


def main() -> int:
    try:
        check_version()
        names = store_extensions()
        installed = ensure(names)
    except (ExtensionError, OSError) as error:
        print(f"duckdb-extensions: {error}")
        return 1
    print(f"duckdb-extensions: {len(names)} needed, {len(installed)} installed now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
