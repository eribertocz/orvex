"""The DuckDB extension installer (tools/ci/duckdb_extensions.py, #240): official files only,
for the pinned DuckDB version, signature verified by DuckDB, failing closed."""

from __future__ import annotations

import gzip
import shutil
import urllib.request
from pathlib import Path

import duckdb
import pytest

from tests.support.environment import unavailable
from tools.ci import duckdb_extensions as tool
from tools.repository import REPOSITORY_ROOT


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


STORE = "packages/store/src/orvex/store/lake.py"


def test_the_extensions_are_read_from_the_store_code() -> None:
    names = tool.store_extensions(REPOSITORY_ROOT)
    assert set(names) == {"icu", "ducklake", "sqlite", "postgres", "azure", "httpfs"}


def test_a_literal_list_and_calls_are_both_read(tmp_path: Path) -> None:
    source = (
        'EXTENSIONS = ("icu", "ducklake")\n'
        "class Lake:\n"
        "    def a(self):\n"
        '        self._load_extension("sqlite")\n'
    )
    assert tool.store_extensions(write(tmp_path, {STORE: source})) == ["ducklake", "icu", "sqlite"]


def test_a_loop_over_the_literal_list_is_read(tmp_path: Path) -> None:
    source = (
        'EXTENSIONS = ("icu", "ducklake")\n'
        "class Lake:\n"
        "    def a(self):\n"
        "        for extension in EXTENSIONS:\n"
        "            self._load_extension(extension)\n"
    )
    assert tool.store_extensions(write(tmp_path, {STORE: source})) == ["ducklake", "icu"]


def test_a_name_that_is_not_a_literal_fails_closed(tmp_path: Path) -> None:
    source = "class Lake:\n    def a(self, name):\n        self._load_extension(name)\n"
    with pytest.raises(tool.ExtensionError, match="EXTENSION_NAME_NOT_LITERAL"):
        tool.store_extensions(write(tmp_path, {STORE: source}))


@pytest.mark.parametrize(
    "source",
    [
        "EXTENSIONS = tuple(names())\n",
        "EXTENSIONS: tuple[str, ...] = tuple(names())\n",
        'EXTENSIONS = ("icu", name)\n',
        "EXTENSIONS += more\n",
    ],
)
def test_any_binding_of_the_list_that_is_not_literal_fails_closed(
    tmp_path: Path, source: str
) -> None:
    with pytest.raises(tool.ExtensionError, match="EXTENSION_NAME_NOT_LITERAL"):
        tool.store_extensions(write(tmp_path, {STORE: source}))


def test_annotated_and_list_forms_are_read(tmp_path: Path) -> None:
    source = 'EXTENSIONS: tuple[str, ...] = ("icu",)\nOTHER = 1\nEXTENSIONS += ["azure"]\n'
    assert tool.store_extensions(write(tmp_path, {STORE: source})) == ["azure", "icu"]


def test_file_names_come_from_duckdb_aliases() -> None:
    assert tool.file_name("sqlite") == "sqlite_scanner"
    assert tool.file_name("postgres") == "postgres_scanner"
    assert tool.file_name("ducklake") == "ducklake"


def test_the_pinned_version_is_read_from_the_lock_of_this_python() -> None:
    assert tool.locked_version(REPOSITORY_ROOT, (3, 14)) == duckdb.__version__
    assert tool.locked_version(REPOSITORY_ROOT, (3, 15)) == duckdb.__version__


def test_another_installed_version_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb, "__version__", "1.9.9")
    with pytest.raises(tool.ExtensionError, match="DUCKDB_VERSION_MISMATCH"):
        tool.check_version(REPOSITORY_ROOT)


@pytest.mark.parametrize(
    "url",
    [
        "http://extensions.duckdb.org/v2.0.0/linux_amd64/icu.duckdb_extension.gz",
        "https://extensions.duckdb.org.evil.example/v2.0.0/linux_amd64/icu.duckdb_extension.gz",
        "https://example.com/v2.0.0/linux_amd64/icu.duckdb_extension.gz",
    ],
)
def test_only_the_official_https_host_is_downloaded(url: str, tmp_path: Path) -> None:
    with pytest.raises(tool.ExtensionError, match="EXTENSION_SOURCE_REFUSED"):
        tool.download(url, tmp_path / "x.gz")


def test_an_oversized_download_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(tool.ExtensionError, match="EXTENSION_TOO_LARGE"):
        tool.copy_bounded(iter([b"x" * 10, b"x" * 10]), tmp_path / "x.gz", limit=15)
    assert not (tmp_path / "x.gz").exists()


def fake_fetch(content: bytes) -> tool.Fetch:
    def fetch(url: str, destination: Path) -> None:
        destination.write_bytes(gzip.compress(content))

    return fetch


def test_a_tampered_file_is_refused_and_removed(tmp_path: Path) -> None:
    directory = tmp_path / "extensions"
    with pytest.raises(tool.ExtensionError, match="EXTENSION_NOT_VERIFIED"):
        tool.ensure(["ducklake"], directory, fake_fetch(b"not an extension"))
    assert not list(directory.rglob("ducklake.duckdb_extension"))


def test_a_signature_broken_by_one_byte_is_refused(tmp_path: Path) -> None:
    official = Path.home() / ".duckdb" / "extensions" / f"v{duckdb.__version__}"
    found = sorted(official.rglob("httpfs.duckdb_extension"))
    if not found:
        unavailable(
            "no official httpfs file installed on this machine to tamper with",
            "the official httpfs extension, for the signature test",
        )
    data = bytearray(found[0].read_bytes())
    data[len(data) // 2] ^= 0xFF
    directory = tmp_path / "extensions"
    with pytest.raises(tool.ExtensionError, match="EXTENSION_NOT_VERIFIED"):
        tool.ensure(["httpfs"], directory, fake_fetch(bytes(data)))
    assert not list(directory.rglob("httpfs.duckdb_extension"))


def test_an_official_file_is_installed_and_loads(tmp_path: Path) -> None:
    official = Path.home() / ".duckdb" / "extensions" / f"v{duckdb.__version__}"
    found = sorted(official.rglob("httpfs.duckdb_extension"))
    if not found:
        pytest.skip("no official httpfs file installed on this machine")
    directory = tmp_path / "extensions"
    installed = tool.ensure(["httpfs"], directory, fake_fetch(found[0].read_bytes()))
    assert installed == ["httpfs"]
    # Installed: a second run downloads nothing.
    assert tool.ensure(["httpfs"], directory, fake_fetch(b"never used")) == []


def test_nothing_is_downloaded_when_every_extension_loads(tmp_path: Path) -> None:
    official = Path.home() / ".duckdb" / "extensions"
    if not (official / f"v{duckdb.__version__}").is_dir():
        pytest.skip("no official extensions installed on this machine")
    directory = tmp_path / "extensions"
    shutil.copytree(official, directory)
    calls: list[str] = []

    def fetch(url: str, destination: Path) -> None:
        calls.append(url)

    names = tool.store_extensions(REPOSITORY_ROOT)
    present = [n for n in names if list(directory.rglob(f"{tool.file_name(n)}.duckdb_extension"))]
    if not present:
        pytest.skip("none of the store's extensions is installed on this machine")
    assert tool.ensure(present, directory, fetch) == []
    assert calls == []


def test_the_download_names_itself_and_refuses_a_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server refuses Python's default user agent; a redirect to another host is refused.
    url = "https://extensions.duckdb.org/v2.0.0/linux_amd64/icu.duckdb_extension.gz"
    seen: list[str] = []

    class Response:
        def __init__(self, final: str) -> None:
            self.final = final

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def geturl(self) -> str:
            return self.final

        def read(self, size: int) -> bytes:
            return b""

    def opener(final: str) -> object:
        def urlopen(request: object, timeout: int) -> Response:
            seen.append(str(getattr(request, "headers", {}).get("User-agent", "")))
            return Response(final)

        return urlopen

    monkeypatch.setattr(urllib.request, "urlopen", opener(url))
    tool.download(url, tmp_path / "ok.gz")
    assert seen == [tool.USER_AGENT]
    monkeypatch.setattr(urllib.request, "urlopen", opener("https://example.com/x.gz"))
    with pytest.raises(tool.ExtensionError, match="EXTENSION_SOURCE_REFUSED"):
        tool.download(url, tmp_path / "redirected.gz")


def test_an_installed_file_that_does_not_load_is_replaced(tmp_path: Path) -> None:
    official = Path.home() / ".duckdb" / "extensions" / f"v{duckdb.__version__}"
    found = sorted(official.rglob("httpfs.duckdb_extension"))
    if not found:
        pytest.skip("no official httpfs file installed on this machine")
    directory = tmp_path / "extensions"
    tool.ensure(["httpfs"], directory, fake_fetch(found[0].read_bytes()))
    installed = next(directory.rglob("httpfs.duckdb_extension"))
    # A new file in its place: the loaded one stays mapped by this process.
    installed.unlink()
    installed.write_bytes(b"damaged")
    assert tool.ensure(["httpfs"], directory, fake_fetch(found[0].read_bytes())) == ["httpfs"]
    assert installed.read_bytes() == found[0].read_bytes()
