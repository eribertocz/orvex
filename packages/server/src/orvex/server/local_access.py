"""Access to the local server (P2-27a, #363; Owner precision 1 and its corrections of
2026-11-07): loopback alone is not enough, and each credential has one purpose.

A serve session holds three kinds of credentials. Each is random (256 bits), compared in
constant time, never logged, and held only in this process, so all of them are revoked when
the server stops.

- **The bootstrap code** opens the browser. It works once, and only for a few minutes. The
  server opens `/?code=<code>` in the browser, or, with `--no-browser`, leaves that link in a
  file only its owner may read; the first request with it gets a new session
  cookie and a redirect to the same page without the code, under `Referrer-Policy:
  no-referrer` and `Cache-Control: no-store`. The code then works no more, so an address kept
  in the history or a log grants nothing.
- **A session cookie** is issued for each exchanged code: `HttpOnly`, `SameSite=Strict`, with
  a name that carries the port, since cookies are not kept apart by port. Its value is its
  own secret, never the bootstrap code or the bearer token.
- **The bearer token** is for scripts: `Authorization: Bearer <token>`. It is written to a
  file only its owner may read, removed when the server stops.

**The limit of this protection.** The file's permissions keep the bearer token from other
users of the machine. They do not isolate the server from a malicious process of the same
user, which can read that user's files and memory; only isolation by the operating system
(another user, a container, a sandbox) can. Nothing here claims more.

What a credential proves is checked here; what the request may do is decided by `access`.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

TOKEN_BYTES = 32
CODE_PARAMETER = "code"
CODE_SECONDS = 300
BEARER = "bearer "


def _same(candidate: object, secret: str) -> bool:
    if not isinstance(candidate, str) or not candidate.isascii():
        return False
    return hmac.compare_digest(candidate.encode(), secret.encode())


class LocalAccess:
    """The credentials of one serve session."""

    def __init__(
        self, cookie: str, clock: Callable[[], float] = time.monotonic, seconds: int = CODE_SECONDS
    ) -> None:
        self.cookie = cookie
        self._clock = clock
        self._code: str | None = secrets.token_urlsafe(TOKEN_BYTES)
        self._code_until = clock() + seconds
        self._bearer = secrets.token_urlsafe(TOKEN_BYTES)
        self._sessions: list[str] = []
        self._lock = threading.Lock()
        self._open = True

    def __repr__(self) -> str:
        return f"LocalAccess(cookie={self.cookie!r})"

    @property
    def code(self) -> str:
        """The bootstrap code, to open the browser with; it works once."""
        if self._code is None:
            raise ValueError("the bootstrap code was already used")
        return self._code

    @property
    def bearer(self) -> str:
        """The scripts' token, for the token file only."""
        return self._bearer

    def exchange(self, candidate: object) -> str | None:
        """A new session for a genuine, unused, unexpired bootstrap code; then the code is spent.
        None otherwise, and a wrong guess spends nothing."""
        with self._lock:
            code = self._code
            if not self._open or code is None or self._clock() > self._code_until:
                return None
            if not _same(candidate, code):
                return None
            self._code = None
            session = secrets.token_urlsafe(TOKEN_BYTES)
            self._sessions.append(session)
            return session

    def accepts(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> bool:
        """Whether a request carries the bearer token or a live session cookie. When an
        `Authorization` header is sent, it alone decides."""
        with self._lock:
            if not self._open:
                return False
            authorization = headers.get("authorization")
            if authorization is not None:
                if not authorization.lower().startswith(BEARER):
                    return False
                return _same(authorization[len(BEARER) :].strip(), self._bearer)
            presented = cookies.get(self.cookie)
            # Every session is compared, so the time taken says nothing of which one matched.
            found = [_same(presented, session) for session in self._sessions]
            return any(found)

    def revoke(self) -> None:
        """Every credential of the session stops working (the server stops)."""
        with self._lock:
            self._open = False
            self._code = None
            self._sessions.clear()


@dataclass(frozen=True)
class Files:
    token: Path
    link: Path


def new_access(slug: str, port: int) -> LocalAccess:
    return LocalAccess(f"{slug}_session_{port}")


def _private(path: Path, text: str) -> None:
    """`text` in a new file readable by its owner only; a file left by a server that died is
    replaced."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, text.encode())
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def serving(directory: Path, port: int, access: LocalAccess, url: str) -> Iterator[Files]:
    """While the server runs: the bearer token in `<directory>/serve-<port>.token` and the
    one-time browser link in `<directory>/serve-<port>.link`, both readable by their owner
    only, so neither secret is printed or logged. At the end both files are removed and every
    credential is revoked."""
    directory.mkdir(parents=True, exist_ok=True)
    files = Files(directory / f"serve-{port}.token", directory / f"serve-{port}.link")
    try:
        _private(files.token, access.bearer)
        _private(files.link, f"{url}?{CODE_PARAMETER}={access.code}\n")
        yield files
    finally:
        access.revoke()
        for path in (files.token, files.link):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
