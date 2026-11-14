"""The administration CLI's HTTP client (#446 Q1 A): it signs in to a server-mode instance as a
person, calls the administration routes like any client, and signs out.

- **The server** is reached over HTTPS; plain HTTP only to the loopback interface.
- **The password** never comes from an argument: the caller passes it from the terminal.
- **The session** is kept only for the one command, in memory: the session cookie is sent back
  as received and never written anywhere. A sign-out ends it, after a failure too.
- **State changes** carry the server's own origin, as the server's request rules require of
  every client (#438).
- **A temporary password.** A sign-in that must change it stops every command but the password
  change with `PASSWORD_CHANGE_REQUIRED`.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
PASSWORD = "/api/v1/auth/password"
TIMEOUT_S = 30


class Refused(Exception):
    """The server's refusal: its code and message, as `http-api` v1 sends them."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, list[str]]
    body: bytes


class Transport(Protocol):
    def __call__(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> Answer: ...


def checked_server(url: str) -> str:
    """`url` as the server's origin: HTTPS, or HTTP to a loopback address only."""
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.path not in {"", "/"}:
        raise ValueError("the server is an origin such as https://bi.example.com")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise ValueError("plain http is refused except to the loopback interface")
    return f"{parts.scheme}://{parts.netloc}"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def network(origin: str) -> Transport:
    """A transport to `origin` through `http.client`, one connection per request."""
    parts = urlsplit(origin)

    def send(method: str, path: str, body: bytes | None, headers: dict[str, str]) -> Answer:
        assert parts.hostname is not None
        connection: http.client.HTTPConnection = (
            http.client.HTTPSConnection(parts.hostname, parts.port, timeout=TIMEOUT_S)
            if parts.scheme == "https"
            else http.client.HTTPConnection(parts.hostname, parts.port, timeout=TIMEOUT_S)
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            found: dict[str, list[str]] = {}
            for name, value in response.getheaders():
                found.setdefault(name.lower(), []).append(value)
            return Answer(response.status, found, response.read())
        finally:
            connection.close()

    return send


class Client:
    """One signed-in session against the instance at `origin`."""

    def __init__(self, origin: str, transport: Transport) -> None:
        self._origin = origin
        self._send = transport
        self._cookie: str | None = None
        self.password_change_required = False

    def sign_in(self, user_name: str, password: str) -> None:
        body = {"user_name": user_name, "password": password}
        answer = self._call("POST", LOGIN, body)
        self.password_change_required = bool(
            json.loads(answer.body).get("password_change_required", False)
        )

    def sign_out(self) -> None:
        if self._cookie is not None:
            try:
                self._call("POST", LOGOUT, {})
            finally:
                self._cookie = None

    def change_password(self, current: str, new: str) -> None:
        self._call("POST", PASSWORD, {"current_password": current, "new_password": new})
        self.password_change_required = False

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, **query: str
    ) -> Any:  # noqa: ANN401 - the route's JSON document
        """An administration route's document (None for `204`)."""
        if self.password_change_required:
            raise Refused(
                403,
                "PASSWORD_CHANGE_REQUIRED",
                "Change the temporary password first: run the `admin password` command.",
            )
        if query:
            path += "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())
        answer = self._call(method, path, body)
        return json.loads(answer.body) if answer.body else None

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> Answer:
        headers = {"Accept": "application/json"}
        data = None
        if method != "GET":
            headers["Origin"] = self._origin
            headers["Sec-Fetch-Site"] = "same-origin"
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self._cookie is not None:
            headers["Cookie"] = self._cookie
        answer = self._send(method, path, data, headers)
        for cookie in answer.headers.get("set-cookie", []):
            pair = cookie.split(";", 1)[0]
            name, _, value = pair.partition("=")
            if name.endswith("_session"):
                self._cookie = pair if value and value != '""' else None
        if answer.status >= 400:
            try:
                error = json.loads(answer.body)["error"]
                raise Refused(answer.status, str(error["code"]), str(error["message"]))
            except ValueError, KeyError, TypeError:
                raise Refused(answer.status, "INTERNAL_ERROR", "Unexpected answer") from None
        return answer


@contextmanager
def signed_in(
    origin: str, transport: Transport, user_name: str, password: Callable[[], str]
) -> Iterator[Client]:
    """A client signed in for the block, signed out after it, whatever happened."""
    client = Client(origin, transport)
    client.sign_in(user_name, password())
    try:
        yield client
    finally:
        with contextlib.suppress(Refused):  # ended already: a deactivation, a password change
            client.sign_out()
